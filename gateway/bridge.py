"""Local Cockpit control surface; Shunt owns credentials, quotas and inference."""
import hmac
import http.client
import json
import math
import os
import re
import threading
import time
import select
import socket
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qs
from forecast import estimate, renewal_scenario, workload_estimates
from routing import choose, profile, session_rows, unavailable, REASONS
from validation import archive, HistoryIndex
from diagnostics import Journal
import readings
from native_auth import NativeAuthUnavailable
from telemetry import fable_windows, capacity
from private_files import atomic_write

from settings import LABELS, CONFIG
HOP = {"host", "connection", "content-length", "transfer-encoding"}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def window(value):
    if not isinstance(value, dict) or not number(value.get("pct")) or not 0 <= value["pct"] <= 100: return None
    return {"pct": value["pct"], "resetsAt": value.get("resetsAt") if number(value.get("resetsAt")) else None}


def event(value):
    if not isinstance(value, dict) or not number(value.get("at")): return None
    if not all(isinstance(value.get(k), str) and re.fullmatch(r"[a-z0-9-]{1,80}", value[k]) for k in ["from", "to"]): return None
    if value.get("reason") not in REASONS: return None
    return {k: value[k] for k in ["from", "to", "at", "reason"]} | {
        "cacheReadTokens": value.get("cacheReadTokens", 0) if number(value.get("cacheReadTokens", 0)) else 0,
        "cacheCreationTokens": value.get("cacheCreationTokens", 0) if number(value.get("cacheCreationTokens", 0)) else 0,
        "thinkingDropped": value.get("thinkingDropped") is True}


class PoolBridge(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = True

    def __init__(self, address, upstream_port, client_token, admin_token, state_path=None, native_auth=None, shared_readings=None):
        super().__init__(address, Handler)
        self.upstream_port, self.client_token, self.admin_token = upstream_port, client_token, admin_token
        self.native_auth = native_auth
        self.shared_readings = Path(shared_readings) if shared_readings else None
        self.lock = threading.RLock()
        self.sessions, self.observations, self.paused_names = {}, {}, set()
        self.traffic = []
        self.recovery = {"restarts": 0, "state": "ready"}
        self.persistence_error = False
        self._last_saved = None
        self.state_path = Path(state_path) if state_path else None
        self.history_path = self.state_path.parent / 'usage-history' if self.state_path else None
        self.history_error = False
        self.journal = Journal(self.state_path.parent / 'incidents.json' if self.state_path else None, atomic_write)
        self.health = {"backend": "starting", "failures": 0, "checkedAt": None, "supervisor": ("systemd" if sys.platform.startswith("linux") else "launchd") if os.environ.get("POOL_MANAGED") == "1" else "standalone"}
        self.last_accounts = []
        self.draining = False
        self.connections = threading.BoundedSemaphore(64)
        self.requests = {}
        self.body_bytes = 0
        self.max_requests, self.main_reserve = 32, 4
        self.max_body_bytes, self.idle_timeout = 256 * 1024 * 1024, 300
        self.history_index = HistoryIndex(self.history_path) if self.history_path else None
        self.monitor_stop = threading.Event()
        self.monitor = threading.Thread(target=self.monitor_requests, daemon=True)
        self.monitor.start()
        if self.state_path:
            for path in [self.state_path, self.state_path.with_suffix(".bak")]:
                try:
                    if path.stat().st_size > 4 * 1024 * 1024: raise ValueError()
                    stored = json.loads(path.read_text())
                    if not isinstance(stored, dict): raise ValueError()
                    sessions = stored.get("sessions", {}) if stored.get("version") == 1 else stored
                    if not isinstance(sessions, dict): raise ValueError()
                    for key, value in list(sessions.items())[-256:]:
                        if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", key) or not isinstance(value, dict): continue
                        stamp = value.get("at")
                        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp): continue
                        names = {k: value.get(k) if isinstance(value.get(k), str) and re.fullmatch(r"[a-z0-9-]{1,80}", value[k]) else None for k in ["active", "pin"]}
                        state = {**names, "mode": "pinned" if value.get("mode") == "pinned" and names["pin"] else "auto", "wrap": value.get("wrap") is not False, "at": stamp,
                                 "thinkingDropped": value.get("thinkingDropped") is True, "interrupted": bool(value.get("interrupted") or value.get("inFlight") or value.get("serving"))}
                        state["requestAt"] = value.get("requestAt", 0) if number(value.get("requestAt", 0)) else 0
                        if isinstance(value.get('connectorIdentity'), str) and re.fullmatch(r'[a-z0-9-]{1,80}', value['connectorIdentity']): state['connectorIdentity'] = value['connectorIdentity']
                        state["profile"] = profile({"model": value.get("profile", {}).get("model"), "output_config": {"effort": value.get("profile", {}).get("effort")}, "thinking": {"type": "enabled" if value.get("profile", {}).get("effort") == "thinking" else "disabled"}}) if isinstance(value.get("profile"), dict) else profile({})
                        if isinstance(value.get("project"), str) and re.fullmatch(r"[\w .-]{1,60}", value["project"]): state["project"] = value["project"]
                        route = value.get("routing")
                        if isinstance(route, dict) and isinstance(route.get("reason"), str) and route["reason"] in REASONS and (route.get("account") is None or isinstance(route.get("account"), str) and re.fullmatch(r"[a-z0-9-]{1,80}", route["account"])):
                            state["routing"] = {"account": route.get("account"), "reason": route["reason"],
                                                "peers": int(route["peers"]) if number(route.get("peers")) and 0 <= route["peers"] <= 255 else 0}
                        history = value.get("history", [value["switch"]] if value.get("switch") else [])
                        state["history"] = [clean for e in history[-50:] if (clean := event(e))] if isinstance(history, list) else []
                        if state["history"]: state["switch"] = state["history"][-1]
                        self.sessions[key] = state
                    if stored.get("version") == 1:
                        observations = stored.get("observations", {})
                        if isinstance(observations, dict):
                            for key, points in list(observations.items())[-100:]:
                                if not isinstance(points, list): continue
                                valid = [{"at": v["at"], "fiveHour": window(v.get("fiveHour")), "sevenDay": window(v.get("sevenDay")), **({"workload": v["workload"]} if isinstance(v.get("workload"), str) and re.fullmatch(r"(opus|sonnet|haiku|other)/(standard|thinking|low|medium|high|max)", v["workload"]) else {})} for v in points[-200:]
                                         if isinstance(v, dict) and number(v.get("at"))]
                                self.observations[key] = valid
                        for row in stored.get("traffic", [])[-300:] if isinstance(stored.get("traffic", []), list) else []:
                            if not isinstance(row, dict) or not number(row.get("at")) or not isinstance(row.get("account"), str) or not re.fullmatch(r"[a-z0-9-]{1,80}", row["account"]) or not isinstance(row.get("workload"), str) or not re.fullmatch(r"(opus|sonnet|haiku|other)/(standard|thinking|low|medium|high|max)", row["workload"]): continue
                            self.traffic.append({"at": row["at"], "account": row["account"], "workload": row["workload"], **{k: row[k] for k in ("inputTokens", "outputTokens", "thinkingTokens", "cacheReadTokens", "cacheCreationTokens", "durationSeconds") if number(row.get(k)) and row[k] >= 0}})
                        self.paused_names = {n for n in (stored.get("paused") if isinstance(stored.get("paused"), list) else []) if isinstance(n, str) and re.fullmatch(r"[a-z0-9-]{1,80}", n)}
                    if path != self.state_path: self.recovery["restoredBackup"] = True
                    self._last_saved = json.dumps({"version": 1, "sessions": self.sessions, "observations": self.observations, "paused": sorted(self.paused_names), "traffic": self.traffic})
                    break
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, TypeError):
                    self.persistence_error = True

    def handle_error(self, request, client_address):
        self.journal.record('stream_interrupted')

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            try: request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 1\r\n\r\n")
            except OSError: pass
            self.shutdown_request(request)
            self.journal.record('request_rejected')
            return
        try: super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try: super().process_request_thread(request, client_address)
        finally: self.connections.release()

    def server_close(self):
        # Keep cancellation running while ThreadingHTTPServer drains handlers.
        super().server_close()
        self.monitor_stop.set()
        self.monitor.join(timeout=2)

    def monitor_requests(self):
        previous = time.monotonic()
        while not self.monitor_stop.wait(.25):
            now = time.monotonic()
            with self.lock: requests = list(self.requests.values())
            if now - previous > 5:
                # A suspended Mac or scheduler pause is not an upstream idle interval.
                for row in requests: row['progress'] = now
            previous = now
            for row in requests:
                reason = None
                try:
                    readable, _, _ = select.select([row['client']], [], [], 0)
                    if readable and row['client'].recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b'': reason = 'client_disconnected'
                except BlockingIOError: pass  # The handler may consume readable bytes before this peek.
                except (OSError, ValueError, TypeError): reason = 'client_disconnected'
                if not reason and time.monotonic() - row['progress'] > self.idle_timeout: reason = 'request_idle_timeout'
                if reason and not row.get('cancelled'):
                    row['cancelled'] = reason
                    upstream = row.get('upstream')
                    for connection in (upstream, row['client']):
                        if connection is None: continue
                        try: connection.shutdown(socket.SHUT_RDWR)
                        except OSError: pass

    def admit(self, handler, length):
        main = handler.path.split('?', 1)[0] == '/v1/messages' and handler.headers.get('x-claude-code-request-class', 'main') == 'main' and not handler.headers.get('x-claude-code-agent-id')
        with self.lock:
            auxiliary = sum(not r['main'] for r in self.requests.values())
            if self.draining or self.health['failures'] >= 3 or len(self.requests) >= self.max_requests or (not main and auxiliary >= max(0, self.max_requests - self.main_reserve)) or self.body_bytes + length > self.max_body_bytes:
                self.journal.record('request_rejected', requests=len(self.requests))
                return None
            row = {'client': handler.connection, 'progress': time.monotonic(), 'bytes': length, 'upstream': None, 'main': main}
            self.requests[id(handler)] = row
            self.body_bytes += length
            return row

    def release(self, handler):
        with self.lock:
            row = self.requests.pop(id(handler), None)
            if row: self.body_bytes -= row['bytes']

    def check_backend(self):
        try:
            value = self.admin(timeout=2)
            if not isinstance(value.get('providers'), list): raise ValueError()
        except (OSError, RuntimeError, ValueError, http.client.HTTPException):
            with self.lock:
                self.health.update(backend='unresponsive', failures=self.health['failures'] + 1, checkedAt=int(time.time() * 1000))
            self.journal.record('backend_unresponsive')
            return False
        with self.lock:
            was_failed = self.health['failures'] > 0
            self.health.update(backend='ready', failures=0, checkedAt=int(time.time() * 1000))
        if was_failed: self.journal.record('backend_recovered')
        return True

    def diagnostics(self):
        with self.lock:
            return {'health': {**self.health, 'draining': self.draining, 'activeRequests': len(self.requests),
                    'requestLimit': self.max_requests, 'bufferedBytes': self.body_bytes,
                    'mainRequests': sum(r['main'] for r in self.requests.values()), 'mainReservedSlots': self.main_reserve,
                    'storageError': self.persistence_error or self.history_error or self.journal.error},
                    'incidents': self.journal.snapshot()[:50]}

    def admin(self, path="/admin/api/pool", body=None, timeout=3):
        conn = http.client.HTTPConnection("127.0.0.1", self.upstream_port, timeout=timeout)
        try:
            conn.request("GET" if body is None else "PATCH", path,
                         None if body is None else json.dumps(body),
                         {"x-shunt-admin-token": self.admin_token, "Content-Type": "application/json"})
            response = conn.getresponse()
            data = json.loads(response.read())
            if response.status != 200 or not isinstance(data, dict):
                raise RuntimeError("Shunt control request failed")
            return data
        finally:
            conn.close()

    def accounts(self):
        rows = []
        admin = self.admin()
        fable = fable_windows(admin)
        for provider in admin.get("providers", []):
            if provider.get("auth") != "claude_oauth":
                continue
            for source in provider.get("accounts", []):
                def window(suffix):
                    pct = source.get("utilization_" + suffix)
                    reset = source.get("reset_" + suffix)
                    return None if pct is None else {"pct": pct * 100, "resetsAt": reset * 1000 if reset else None}
                status = ("reauthenticate" if source.get("needs_relogin") else
                          "paused" if source.get("paused") or source.get("disabled") else
                          "limited" if source.get("near_quota") or source.get("capped") else
                          "cooling" if source.get("cooldown_secs_remaining") else
                          "unknown" if not source.get("has_state") else "ready")
                rows.append({"id": source["account_ref"], "name": source["name"],
                             "label": LABELS.get(source["name"], source["name"]), "provider": provider["provider"],
                             "quotaLimits": {key: {"status": source.get("status_" + suffix), "resetsAt": source.get("reset_" + suffix) * 1000 if source.get("reset_" + suffix) else None}
                                             for key, suffix in [('fiveHour', '5h'), ('sevenDay', '7d'), ('fableWeekly', '7d_oi')]},
                             "fiveHour": window("5h"), "sevenDay": window("7d"), "fableWeekly": fable.get((provider['provider'], source['name'], source['account_ref'])), "status": status,
                             "plan": source.get("plan"), "seenAt": (source.get("observed_at_5h") or 0) * 1000,
                             "paused": source.get("paused", False), "cooldownSeconds": source.get("cooldown_secs_remaining", 0) or 0})
        if self.shared_readings: readings.merge(rows, readings.load_shared(self.shared_readings), int(time.time() * 1000))
        from account_metadata import decorate
        decorate({'accounts': rows}, CONFIG)
        with self.lock:
            self.last_accounts = rows
            changed = False
            for row in rows:
                points = self.observations.setdefault(row["id"], [])
                if row["seenAt"] > 0 and (not points or row["seenAt"] > points[-1]["at"]):
                    groups = {r["workload"] for r in self.traffic if r["account"] == row["name"] and (points[-1]["at"] if points else row["seenAt"]) < r["at"] <= row["seenAt"]}
                    groups.update(v.get("profile", {}).get("workload") for v in self.sessions.values() if v.get("inFlight") and (v.get("serving") or v.get("reserved")) == row["name"])
                    tagged = {"workload": next(iter(groups))} if len(groups) == 1 and None not in groups else {}
                    points.append({"at": row["seenAt"], "fiveHour": row["fiveHour"], "sevenDay": row["sevenDay"], **tagged})
                    self.observations[row["id"]] = points[-200:]
                    changed = True
            if changed:
                self.save()
                if self.history_path:
                    try:
                        now = int(time.time() * 1000)
                        archive(self.history_path, rows, self.forecast(rows, now), now)
                        self.history_error = False
                    except (OSError, ValueError): self.history_error = True
        return rows

    def validation(self):
        self.accounts()
        report = self.history_index.report() if self.history_index else {}
        if report.get('indexRebuilt'): self.journal.record('history_index_rebuilt')
        return {**report, 'historyError': self.history_error}

    def restore_pauses(self):
        for account in self.accounts():
            if account["name"] in self.paused_names:
                self.admin(f"/admin/api/pool/{account['provider']}/accounts/{account['id']}", {"paused": True})

    def display_accounts(self):
        try: return self.accounts()
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            with self.lock:
                self.health['backend'] = 'unresponsive'
                return [{**a, 'status': 'unknown'} for a in self.last_accounts]

    def overview(self):
        accounts = self.display_accounts()
        now = int(time.time() * 1000)
        with self.lock:
            histories = [{**e, "session": key} for key, state in self.sessions.items() for e in state.get("history", [])]
            return {"version": "0.8.0", "accounts": accounts, "updatedAt": now, "ready": sum(a["status"] == "ready" for a in accounts),
                    "forecast": self.forecast(accounts, now), "sessions": session_rows(self.sessions, now / 1000), "history": sorted(histories, key=lambda e: e["at"], reverse=True)[:50],
                    "recovery": dict(self.recovery), "persistenceError": self.persistence_error or self.history_error, **self.diagnostics()}

    def session(self, session_id):
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if v.get("inFlight") or time.time() - v.get("at", 0) < 86400}
            if session_id not in self.sessions and len(self.sessions) >= 256:
                idle = [k for k, v in self.sessions.items() if not v.get("inFlight")]
                if not idle: raise RuntimeError("All session slots are in use; retry after a response finishes")
                oldest = min(idle, key=lambda k: self.sessions[k].get("at", 0))
                self.sessions.pop(oldest)
            return self.sessions.setdefault(session_id, {"mode": "auto", "active": None, "wrap": True, "at": time.time()})

    def snapshot(self, session_id):
        accounts = self.display_accounts()
        with self.lock:
            self.session(session_id).update(uiAt=time.time(), at=time.time())
            state = dict(self.session(session_id))
        now = int(time.time() * 1000)
        return {**state, "active": state.get("serving") or state.get("active"), "session": session_id, "accounts": accounts,
                "updatedAt": now, "ready": sum(a["status"] == "ready" for a in accounts),
                "forecast": self.forecast(accounts, now), "sessions": session_rows(self.sessions, now / 1000), "history": list(reversed(state.get("history", []))),
                "recovery": dict(self.recovery), "persistenceError": self.persistence_error or self.history_error, **self.diagnostics()}

    def forecast(self, accounts, now):
        result = estimate(accounts, self.observations, now)
        result["renewals"] = renewal_scenario(accounts, result, now)
        result["workloads"] = workload_estimates(accounts, self.observations, self.traffic, now)
        return result

    def reserve(self, session_id, request):
        accounts = self.accounts()
        with self.lock:
            now = time.time()
            state = self.session(session_id)
            request_profile = profile(request)
            capacity = estimate(accounts, self.observations, int(now * 1000))
            learned = next((w for w in workload_estimates(accounts, self.observations, self.traffic, int(now * 1000)) if w["workload"] == request_profile["workload"]), None)
            calibrated = set()
            if learned:
                for row in capacity["accounts"]:
                    account = next(a for a in accounts if a["name"] == row["name"])
                    times = []
                    for w in learned["windows"]:
                        key = "fiveHour" if w["window"] == "5h" else "sevenDay"
                        current = account.get(key)
                        if w["account"] == row["name"] and current and w["ratePerHour"] > 0:
                            times.append(max(0, 95 - current["pct"]) * 3600 / w["ratePerHour"])
                    if times:
                        row["seconds"] = min(times)
                        calibrated.add(row["name"])
            decision = choose(accounts, self.sessions, session_id, capacity, now)
            decision["basis"] = request_profile["workload"] + " quota pace" if decision["account"] in calibrated else "recent account pace"
            context = {"profile": request_profile, "routing": decision, "startedAt": now}
            state.update(profile=context["profile"], routing=decision, requestAt=now, reserved=decision["account"], inFlight=int(state.get("inFlight", 0)) + 1)
            self.save()
            return context

    def save(self):
        if not self.state_path: return
        try:
            if self._last_saved is not None: atomic_write(self.state_path.with_suffix(".bak"), self._last_saved)
            data = json.dumps({"version": 1, "sessions": self.sessions, "observations": self.observations, "paused": sorted(self.paused_names), "traffic": self.traffic})
            atomic_write(self.state_path, data)
            self._last_saved = data
            self.persistence_error = False
        except (OSError, ValueError):
            self.persistence_error = True
            self.journal.record('storage_failed')

    def served(self, session_id, account, usage, transformations, context=None):
        if not session_id or not account:
            return
        with self.lock:
            state = self.session(session_id)
            previous = state.get("active")
            route = (context or {}).get("routing", state.get("routing", {}))
            reason = route.get("reason", "manual selection" if state.get("pin") == account else "quota or upstream failover")
            if route.get("account") and route["account"] != account: reason = "upstream failover"
            state["routing"] = {**route, "account": account, "reason": reason}
            if context:
                state["profile"] = context["profile"]
                details = usage.get("output_tokens_details")
                self.traffic.append({"at": int(time.time() * 1000), "account": account, "workload": context["profile"]["workload"],
                    "durationSeconds": max(0, time.time() - context["startedAt"]),
                    "thinkingTokens": details["thinking_tokens"] if isinstance(details, dict) and number(details.get("thinking_tokens")) else None,
                    **{target: usage.get(source, 0) if number(usage.get(source, 0)) else 0 for source, target in [("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"), ("cache_read_input_tokens", "cacheReadTokens"), ("cache_creation_input_tokens", "cacheCreationTokens")]}})
                self.traffic = self.traffic[-300:]
            if previous and previous != account:
                state["switch"] = {"from": previous, "to": account, "at": int(time.time() * 1000),
                                   "reason": reason,
                                   "cacheReadTokens": usage.get("cache_read_input_tokens", 0),
                                   "cacheCreationTokens": usage.get("cache_creation_input_tokens", 0),
                                   "thinkingDropped": any(t.get("type") == "thinking_dropped" for t in transformations)}
                state["history"] = (state.get("history", []) + [dict(state["switch"])])[-50:]
            dropped = any(t.get("type") == "thinking_dropped" for t in transformations)
            state.pop("serving", None)
            state.update(interrupted=False, active=account, at=time.time(), usage={k: v for k, v in usage.items() if k in {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"} and number(v)},
                         thinkingDropped=dropped or (previous == account and state.get("thinkingDropped", False)))
            if isinstance(usage.get("output_tokens_details"), dict) and number(usage["output_tokens_details"].get("thinking_tokens")):
                state["usage"]["output_tokens_details"] = {"thinking_tokens": usage["output_tokens_details"]["thinking_tokens"]}
            self.save()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15

    def log_message(self, *args):
        pass

    def authorized(self):
        self.native_identity = None
        if len(self.headers.get_all('Authorization', [])) != 1: return False
        authorization = self.headers.get('Authorization', '')
        if hmac.compare_digest(authorization.encode(), ('Bearer ' + self.server.client_token).encode()): return True
        if self.server.native_auth:
            self.native_identity = self.server.native_auth.authenticate(authorization)
            if self.native_identity:
                # Shunt receives only its local client credential, never the connector login.
                self.headers.replace_header('Authorization', 'Bearer ' + self.server.client_token)
                return True
        return False

    def answer(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def control(self):
        session_id = self.path.removeprefix("/cockpit/")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,80}", session_id):
            self.answer(400, {"error": "Invalid session"})
            return
        try:
            project = unquote(self.headers.get("x-cockpit-project", ""))
            if re.fullmatch(r"[\w .-]{1,60}", project):
                with self.server.lock: self.server.session(session_id)["project"] = project
            if self.command == "POST":
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    self.answer(400, {"error": "Invalid control body"})
                    return
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict): raise ValueError()
                action = body.get("action")
                account = next((a for a in self.server.accounts() if a["name"] == body.get("account")), None)
                if action in {"pin", "pause", "resume"} and account is None:
                    self.answer(400, {"error": "Select a configured account"})
                    return
                if action in {"pause", "resume"}:
                    self.server.admin(f"/admin/api/pool/{account['provider']}/accounts/{account['id']}", {"paused": action == "pause"})
                    with self.server.lock:
                        if action == "pause": self.server.paused_names.add(account["name"])
                        else: self.server.paused_names.discard(account["name"])
                        self.server.save()
                elif action == "wrap":
                    if not isinstance(body.get("enabled"), bool):
                        self.answer(400, {"error": "Wrap requires a boolean"})
                        return
                    with self.server.lock:
                        self.server.session(session_id)["wrap"] = body["enabled"]
                        self.server.save()
                elif action in {"pin", "unpin"}:
                    with self.server.lock:
                        state = self.server.session(session_id)
                        state.update(mode="pinned" if action == "pin" else "auto", pin=body.get("account") if action == "pin" else None)
                        self.server.save()
                else:
                    self.answer(400, {"error": "Unknown control action"})
                    return
            self.answer(200, self.server.snapshot(session_id))
        except (OSError, ValueError, RuntimeError):
            self.answer(503, {"error": "Unable to confirm pool state; refresh before retrying"})

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        try: authorized = self.authorized()
        except NativeAuthUnavailable:
            self.answer(503, {'error':'Native login validation temporarily unavailable; retry shortly'})
            return
        if not authorized:
            self.answer(401, {"error": "Authentication required"})
            return
        if self.command == "GET" and self.path == "/pool/status":
            self.answer(200, {"version": "0.8.0", "streamRecovery": "framed-api-error-v1", "modelReporting": "completed-request-v1", "quotaRecovery": "quota-wait-v1", "recovery": self.server.recovery, **self.server.diagnostics()})
            return
        if self.command == "GET" and self.path == "/pool/overview":
            try: self.answer(200, self.server.overview())
            except (OSError, ValueError, RuntimeError): self.answer(503, {"error": "Pool recovering; retry shortly"})
            return
        if self.command == "GET" and self.path == "/pool/validation":
            try: self.answer(200, self.server.validation())
            except (OSError, ValueError, RuntimeError, sqlite3.Error): self.answer(503, {"error": "History unavailable; retry shortly"})
            return
        if self.command == 'GET' and urlsplit(self.path).path == '/pool/capacity':
            model = parse_qs(urlsplit(self.path).query).get('model', ['unknown'])[0]
            if not re.fullmatch(r'[A-Za-z0-9._:\[\]/-]{1,160}', model):
                self.answer(400, {'error': 'Invalid model'}); return
            try:
                state = capacity(self.server.accounts(), model, int(time.time() * 1000))
                if state['state'] == 'quota_wait' and parse_qs(urlsplit(self.path).query).get('wait') == ['1']:
                    delay = min(20, max(.25, (state['nextResetAt'] - state['checkedAt']) / 1000)) if state['nextResetAt'] else 20
                    # Long polling uses no inference admission slot or model traffic.
                    self.server.monitor_stop.wait(delay)
                    state = capacity(self.server.accounts(), model, int(time.time() * 1000))
                self.answer(200, state)
            except (OSError, ValueError, RuntimeError, http.client.HTTPException):
                self.answer(503, {'error': 'Quota availability could not be confirmed'})
            return
        if self.command == 'GET' and self.path == '/pool/diagnostics':
            self.answer(200, self.server.diagnostics())
            return
        if self.command == "POST" and self.path == "/pool/shutdown":
            self.server.draining = True
            self.server.journal.record('service_draining', requests=len(self.server.requests))
            self.answer(200, {"status": "stopping"})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path.startswith("/cockpit/"):
            self.control()
            return
        path = self.path.split("?", 1)[0]
        if path not in {"/v1/messages", "/v1/messages/count_tokens", "/v1/models", "/protocol", "/health", "/usage", "/api/oauth/usage"}:
            self.answer(404, {"error": "Unknown endpoint"})
            return
        try: length = int(self.headers.get("Content-Length", "0")) if self.command == "POST" else 0
        except ValueError:
            self.answer(400, {"error": "Invalid request body"}); return
        if self.command == "POST" and not 0 < length <= 128 * 1024 * 1024:
            self.answer(400, {"error": "Invalid request body"}); return
        lease = self.server.admit(self, length)
        if lease is None:
            reason = 'Pool draining' if self.server.draining else 'Pool backend recovering' if self.server.health['failures'] >= 3 else 'Pool request capacity reached'
            self.answer(503, {"error": reason + '; retry shortly'}); return
        try: self.proxy(path, length, lease)
        finally: self.server.release(self)

    def proxy(self, path, length, lease):
        body, request = None, {}
        session_id = self.headers.get("x-claude-code-session-id", "")
        if session_id and not re.fullmatch(r"[A-Za-z0-9-]{1,80}", session_id):
            self.answer(400, {"error": "Invalid session"})
            return
        headers = {k.lower(): v for k, v in self.headers.items() if k.lower() not in HOP | {"x-shunt-session-account", "x-cockpit-project"}}
        headers["accept-encoding"] = "identity"
        if self.command == "POST":
            try:
                body = self.rfile.read(length)
                if len(body) != length: raise ValueError()
                request = json.loads(body)
                if not isinstance(request, dict):
                    raise ValueError()
            except ValueError:
                self.answer(400, {"error": "Invalid request body"})
                return
        main = lease['main']
        tracked = main and bool(session_id)
        context = None
        if tracked:
            try: context = self.server.reserve(session_id, request or {})
            except (OSError, ValueError, RuntimeError):
                self.answer(503, {"error": "Unable to confirm pool accounts; retry shortly"})
                return
            preferred = context["routing"]["account"]
            if self.native_identity:
                with self.server.lock: self.server.session(session_id)['connectorIdentity'] = self.native_identity
            if preferred: headers["x-shunt-session-account"] = preferred
            beta = headers.get("anthropic-beta", "")
            headers["anthropic-beta"] = ",".join(dict.fromkeys([v for v in beta.split(",") if v] + ["thinking-binding-controls-2026-08-01"]))
        complete, sse = False, False
        conn = http.client.HTTPConnection("127.0.0.1", self.server.upstream_port, timeout=300)
        committed, stream_error, account = False, False, None
        response = None
        try:
            conn.connect()
            lease['upstream'] = conn.sock
            if lease.get('cancelled'): raise ConnectionError()
            conn.request(self.command, self.path, body, headers)
            response = conn.getresponse()
            lease['progress'] = time.monotonic()
            account = response.getheader('x-shunt-account')
            if response.status >= 400: self.server.journal.record('upstream_http_error', status=response.status, account=account)
            while response.status == 429 and path == '/v1/messages' and request.get('stream') is True and isinstance(request.get('model'), str):
                quota = capacity(self.server.accounts(), request.get('model', ''), int(time.time() * 1000))
                if quota['state'] != 'quota_wait': break
                # This request was rejected before any model output or tool call.
                response.close(); conn.close(); lease['upstream'] = None
                if not committed:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    committed, sse = True, True
                if tracked:
                    with self.server.lock:
                        self.server.session(session_id)['quotaWaiting'] = {'since': int(time.time() * 1000), 'resetsAt': quota['nextResetAt']}
                        self.server.save()
                next_check, next_ping = time.monotonic(), 0
                while quota['state'] == 'quota_wait':
                    if self.server.draining or lease.get('cancelled'): raise ConnectionError()
                    now = time.monotonic()
                    if now >= next_ping:
                        self.wfile.write(b'event: ping\ndata: {"type":"ping"}\n\n'); self.wfile.flush()
                        lease['progress'] = now
                        next_ping = now + 10
                    if now >= next_check:
                        quota = capacity(self.server.accounts(), request.get('model', ''), int(time.time() * 1000))
                        delay = min(20, max(.25, (quota['nextResetAt'] - quota['checkedAt']) / 1000)) if quota['nextResetAt'] else 20
                        next_check = now + delay
                    if quota['state'] == 'quota_wait': self.server.monitor_stop.wait(.25)
                if tracked:
                    with self.server.lock:
                        self.server.session(session_id).pop('quotaWaiting', None)
                        self.server.save()
                if lease.get('cancelled') or self.server.draining: raise ConnectionError()
                if context: context['startedAt'] = time.time()
                conn.connect(); lease['upstream'] = conn.sock
                conn.request(self.command, self.path, body, headers)
                response = conn.getresponse()
                lease['progress'] = time.monotonic()
                account = response.getheader('x-shunt-account')
            if committed and (response.status != 200 or 'text/event-stream' not in response.getheader('Content-Type', '')):
                raise ConnectionError()
            if not committed:
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()
                committed = True
            if main and response.status == 200 and session_id:
                with self.server.lock:
                    state = self.server.session(session_id)
                    actual = response.getheader("x-shunt-account")
                    state["serving"] = actual
                    if actual and state.get("routing", {}).get("account") != actual:
                        state["routing"].update(account=actual, reason="upstream failover")
                    self.server.save()
            sse = "text/event-stream" in response.getheader("Content-Type", "") and response.status == 200
            pending, usage, transformations, complete = b"", {}, [], False
            while chunk := response.read1(65536):
                lease['progress'] = time.monotonic()
                if not sse:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    continue
                pending += chunk
                # Hold an incomplete SSE frame so a transport error remains valid JSON.
                while match := re.search(br"\r?\n\r?\n", pending):
                    frame, pending = pending[:match.end()], pending[match.end():]
                    if len(frame) > 1024 * 1024: raise ValueError('SSE frame too large')
                    for line in frame.splitlines():
                        if not line.startswith(b"data: "):
                            continue
                        try: event = json.loads(line[6:])
                        except ValueError: continue
                        if not isinstance(event, dict): continue
                        if event.get('type') == 'error':
                            stream_error = True
                            self.server.journal.record('stream_error', account=account)
                        if event.get("type") == "message_start":
                            message = event.get("message", {})
                            usage.update(message.get("usage", {}))
                            transformations = message.get("input_transformations", [])
                        if event.get("type") == "message_delta":
                            usage.update(event.get("usage", {}))
                            transformations += event.get("input_transformations", [])
                        if event.get("type") == "message_stop": complete = True
                    self.wfile.write(frame)
                    self.wfile.flush()
                    if main and complete and not stream_error and session_id:
                        self.server.served(session_id, account, usage, transformations, context)
                        main = False
                    if complete or stream_error: break
                if len(pending) > 1024 * 1024: raise ValueError('SSE frame too large')
                # Terminal events release the slot even if upstream leaves its socket open.
                if complete or stream_error: break
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            if not lease.get('cancelled'):
                self.server.journal.record('stream_interrupted' if committed else 'upstream_unavailable', account=account)
            if not committed:
                try: self.answer(502, {"type":"error", "error":{"type":"api_error", "message":"Upstream unavailable; retry shortly"}})
                except OSError: pass
        finally:
            if committed and sse and not complete and not stream_error and not lease.get('cancelled'):
                # Native owns retries; never replay a partial response or synthesize success.
                error = {'type':'error', 'error':{'type':'api_error', 'message':'Upstream response interrupted before completion; retry this request'}}
                try:
                    self.wfile.write(b'event: error\ndata: ' + json.dumps(error).encode() + b'\n\n')
                    self.wfile.flush()
                except OSError: pass
            if lease.get('cancelled'): self.server.journal.record(lease['cancelled'], account=account)
            elif tracked and not complete: self.server.journal.record('stream_interrupted', account=account)
            if tracked:
                with self.server.lock:
                    state = self.server.session(session_id)
                    state["inFlight"] = max(0, int(state.get("inFlight", 1)) - 1)
                    if not state["inFlight"]:
                        state.pop("inFlight", None)
                        state.pop("serving", None)
                        state.pop("reserved", None)
                        state.pop("quotaWaiting", None)
                    if not complete or stream_error or lease.get('cancelled'): state["interrupted"] = True
                    self.server.save()
            if response is not None: response.close()
            conn.close()
            self.close_connection = True
