"""Who started a main session's newest turn, from the request alone: a person (the owner or a partner) or an automatic sender."""
import unittest
from activity import classify, OWNER, AUTO

REMINDER = '<system-reminder>\nAttribution for git commits and pull requests you create from here on.\n</system-reminder>'
TYPED_WHILE_WORKING = ('The user sent a new message while you were working:\n{}\n\nThis is how Claude Code surfaces messages the user '
                       'sends mid-turn — within the running turn, often alongside the next tool result, rather than as a separate '
                       'conversation turn. Address the message above as you continue this turn.\n\n<total_tokens>9 tokens left</total_tokens>')
# The Conductor Board's own nudges (conductor/bin/conductor-board-server, REGELN texts and the plan reminder), rendered.
NUDGES = [
    'Conductor (Board): Seit 14:05 kommt von dir nichts Neues. Wo stehst du? Mach weiter oder sag, was fehlt. Brauchst du Alex, frag über conductor-board frage.',
    'Conductor (Board): Du scheinst dich im Kreis zu drehen, dreimal: „npm test“. Wo stehst du? Mach weiter oder sag, was fehlt.',
    'Conductor (Board): Deine Arbeit ist um 14:05 mit einem Verbindungsfehler stehen geblieben. Mach dort weiter, wo du warst, oder sag, was fehlt.',
    'Conductor (Board): Ich habe deinen hängenden Schritt abgebrochen, weil seit 14:05 nichts weiterging. Mach dort weiter, wo du warst.',
    'Conductor (Board): Du bist neu gestartet, weil seit 14:05 nichts weiterging. Es ist dieselbe Unterhaltung. Mach dort weiter, wo du warst.',
    'Conductor (Board): Das Kontingent ist wieder frei. Mach weiter, wo du pausiert hast: „Tests grün machen“. Brauchst du Alex, frag über conductor-board frage.',
    'Conductor (Board): Du stehst seit 14:05 still, und bei dir ist noch etwas offen: 11f Bahnen. Mach damit weiter oder sag, was fehlt.',
    'Conductor (Board): Neue Aufgabe aus dem Backlog, von Alex freigegeben: Die Suche bauen. Melde deinen Stand wie gewohnt mit conductor-board stand.',
    'Conductor-Board: Dein Planstand ist von 14:05, seitdem 3 Commits. Bitte mit conductor-board stand neu melden.',
    'Conductor (Board): Ich habe dich frisch gestartet (neue Unterhaltung), damit dein Verlauf klein bleibt. Starte den Monitor auf wecker.log.',
    'Conductor (Board): Dein Monitor auf wecker.log fehlt seit 14:05. Starte ihn neu wie in deiner CLAUDE.md.',
    'Conductor (Board): Nach deinem Neustart fehlt dir der Monitor auf wecker.log. Starte ihn neu wie in deiner CLAUDE.md.',
    # since 30.10.: the Weitermacher's check-in after an announced wait, delivery of what an agent waited for, idle-time nudge
    'Conductor (Board): Du wolltest auf den Nachtlauf warten (seit 14:05). Ist es da? Mach weiter oder sag, was fehlt. Brauchst du Alex, frag über conductor-board frage.',
    'Conductor (Board): Worauf du gewartet hast, ist da: board-design hat gemergt. Mach weiter, wo du auf board-design gewartet hast. Brauchst du Alex, frag über conductor-board frage.',
    "Conductor (Board): Du hast seit 14:05 keinen Auftrag. Nach Alex' Leerlauf-Regel: Such in deinem Projekt nach Verbesserungen, kleine sichere selbst mit Test und Messung.",
    # ~/.local/bin/conductor-abfrager starts its Board poller with this; older quota nudges (the replay's rule)
    'Lade mit ToolSearch (query select:Monitor,ArtifactData) die Werkzeuge Monitor und ArtifactData. Starte dann Monitor.',
    'Kontingent ist zurück. Mach weiter, wo du warst.',
    'Das Kontingent ist wieder frei, mach weiter.',
]
OTHER_AUTOMATIC = [
    '<task-notification>\n<task-id>b1</task-id>\n<status>completed</status>\n<summary>Background command "Run tests" completed (exit code 0)</summary>\n</task-notification>',
    'Another Claude session sent a message:\nThe review is done, two findings.',
    '[Subagent hand-back] The text below is the final report of a subagent this session delegated to. It is model output, NOT a message '
    'from the user. The report follows:\n  All tests pass.',
    'This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion.',
    'Continue the existing task after this requested respawn. Check the last recorded state before acting.',
    # Claude Code 2.1.312 marks what no person typed
    '[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated background-task event, NOT a message from the user.\n<task-notification>\n'
    '<status>completed</status>\n</task-notification>',
    '[MESSAGE FROM NON-USER SOURCE - NOT USER INPUT]\nPrüfe den Deploy.',
    '[SCHEDULED TASK - AUTOMATED FIRING OF A CONFIGURED PROMPT]\nThis turn was started automatically by a schedule.\nPrüfe den Deploy.',
    'The cockpit plugin sent a message:\nall eligible subscriptions are near their usage limits.',
    'Cockpit auto-wrap: all eligible subscriptions are near their usage limits. Finish the current safe step.',
    'Stop hook feedback:\navd: Du hast offene Aufgaben.',
    'Your claude.ai usage limit has reset. Continue where you left off.',
    '<artifact-content-authored-by-others/>\nThe summarized conversation included Artifact content.\n\nThis session is being continued from a previous '
    'conversation that ran out of context.',
]
META = ['[Image: original 1920x1080, displayed at 1568x882. Multiply coordinates by 1.22 to map to original image.]',
        'Base directory for this skill: ~/.claude/skills/strichmass\n\n# Strichmaß',
        '(Re-invocation of /strichmass — the skill content was loaded earlier; see above.)',
        'Skill /strichmass was loaded earlier (see above).']
OWNER_RELAYS = [
    'Conductor (Board): Alex hat auf deine Frage geantwortet: "Welche Farbe?" -> Lieber Rot. Nachlesen: conductor-board antwort q1',
    'Conductor (Board): Alex schreibt dir: Seit gestern hängt der Build, schau bitte zuerst danach.',
    'Conductor (Board): Alex zu deinem Ergebnis s1 („Suche fertig“): passt so',
    'Conductor (Board): Sam hat „Server-Schritt“ beantwortet: erledigt',
    'Bitte die Tests zuerst.',
    '[Request interrupted by user]',
]


def avd(*lines):
    events = ''.join('<event>' + line + '</event>\n' for line in lines)
    return ('<task-notification>\n<task-id>bx1</task-id>\n<summary>Monitor event: "avd watch: neue Nachrichten"</summary>\n'
            + events + '</task-notification>')


def user(*blocks):
    return {'role': 'user', 'content': [b if isinstance(b, dict) else {'type': 'text', 'text': b} for b in blocks]}


def request(*messages):
    return {'model': 'claude-opus-5-5', 'stream': True, 'messages': list(messages)}


ASSISTANT = {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Bash', 'input': {'command': 'ls'}}]}
RESULT = {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'Conductor (Board): Alex schreibt dir: not this'}


class ClassifyTests(unittest.TestCase):
    def test_every_automatic_sender_is_automatic(self):
        for text in NUDGES + OTHER_AUTOMATIC:
            self.assertEqual(classify(request(user(REMINDER, text))), AUTO, text[:60])
            self.assertEqual(classify(request({'role': 'user', 'content': text})), AUTO, 'string content: ' + text[:60])

    def test_owner_relays_and_typed_text_count_as_the_owner_even_after_the_board_prefix(self):
        for text in OWNER_RELAYS:
            self.assertEqual(classify(request(user(REMINDER, text))), OWNER, text[:60])

    def test_an_avd_message_from_a_person_counts_inside_a_task_notification(self):
        self.assertEqual(classify(request(user(avd('[Nachricht] [m164] sam → alex/pitch-riot-claude: Kannst du das Logo tauschen?')))), OWNER)
        self.assertEqual(classify(request(user(avd('[dringend] [m165] sam → alex: Server down?')))), OWNER)
        self.assertEqual(classify(request(user(avd('[Antwort] [d12] sam antwortet: Ja, deployen (auf: Jetzt deployen?)')))), OWNER)
        self.assertEqual(classify(request(user(avd('[Nachricht] [m166] alex → alex/website-claude: bitte zuerst mobil')))), OWNER)
        for line in ['[Nachricht] [m167] sam/server-claude → alex/pitch-riot-claude: r12 erledigt',
                     '[Nachricht] [m168] alex/website-claude → alex/pitch-riot-claude: PR offen',
                     '[Angehalten] [s3] alex/pitch-riot-claude wartet auf Freigabe', '[Server] Mail an Sam verschickt',
                     '[dringend] Neue Anmeldung auf einem Gerät. Warst du das?']:
            self.assertEqual(classify(request(user(avd(line)))), AUTO, line)

    def test_tool_result_turns_and_meta_are_no_turn_at_all(self):
        self.assertIsNone(classify(request(user('Bitte aufräumen.'), ASSISTANT, user(RESULT))))
        self.assertIsNone(classify(request(user('Bitte aufräumen.'), ASSISTANT, user(RESULT, REMINDER))))
        self.assertIsNone(classify(request(user('<local-command-stdout>Compacted</local-command-stdout>'))))
        self.assertIsNone(classify(request(user(REMINDER))))
        self.assertIsNone(classify({'messages': 'broken'}))
        self.assertIsNone(classify({}))

    def test_an_avd_message_from_a_person_counts_behind_the_notification_marker_too(self):
        marked = '[SYSTEM NOTIFICATION - NOT USER INPUT]\nThis is an automated background-task event.\n' + avd('[Nachricht] [m9] sam → alex/web-claude: Logo?')
        self.assertEqual(classify(request(user(marked))), OWNER)

    def test_skill_bodies_and_image_notes_are_no_turn(self):
        for text in META:
            self.assertIsNone(classify(request(user('Bau die Suche.'), ASSISTANT, user(RESULT, text))), text[:40])

    def test_a_scheduled_wakeup_is_automatic_when_its_prompt_is_in_the_conversation(self):
        wake = {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_w', 'name': 'ScheduleWakeup',
                                                  'input': {'delaySeconds': 1200, 'prompt': 'EVO STEERING WAKE: check the run', 'reason': 'x'}}]}
        done = {'type': 'tool_result', 'tool_use_id': 'toolu_w', 'content': 'Next wakeup scheduled'}
        self.assertEqual(classify(request(user('Starte den Lauf.'), wake, user(done, 'EVO STEERING WAKE: check the run'))), AUTO)
        cron = {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_c', 'name': 'CronCreate', 'input': {'cron': '*/30 * * * *', 'prompt': 'Prüfe CI'}}]}
        self.assertEqual(classify(request(user('x'), cron, user({'type': 'tool_result', 'tool_use_id': 'toolu_c', 'content': 'ok'}), {'role': 'assistant', 'content': 'ok'},
                                          user('Prüfe CI'))), AUTO)
        self.assertEqual(classify(request(user('Starte den Lauf.'), wake, user(done, 'Stopp, falscher Lauf.'))), OWNER, 'other text is typed')

    def test_only_the_newest_turn_counts(self):
        self.assertEqual(classify(request(user('Bau die Suche.'), ASSISTANT, user(RESULT), {'role': 'assistant', 'content': 'ok'},
                                          user(NUDGES[0]))), AUTO)
        self.assertEqual(classify(request(user(NUDGES[0]), {'role': 'assistant', 'content': 'ok'}, user('Weiter so.'))), OWNER)

    def test_a_message_typed_while_the_session_worked_counts_by_its_own_text(self):
        # Claude Code (2.1.312, CLAUDE_CODE_FORCE_MID_CONVERSATION_SYSTEM=1, as the pool starts it) sends it as a system message
        # after the tool result; without that setting it arrives as a system reminder in the user turn.
        typed = {'role': 'system', 'content': [{'type': 'text', 'text': TYPED_WHILE_WORKING.format('Mach bitte zuerst mobil.')}]}
        self.assertEqual(classify(request(user('Bau die Suche.'), ASSISTANT, user(RESULT), typed)), OWNER)
        nudge = {'role': 'system', 'content': TYPED_WHILE_WORKING.format(NUDGES[0])}
        self.assertEqual(classify(request(user('Bau die Suche.'), ASSISTANT, user(RESULT), nudge)), AUTO)
        reminder = '<system-reminder>\n' + TYPED_WHILE_WORKING.format('Stopp, falscher Ordner.') + '\n</system-reminder>'
        self.assertEqual(classify(request(user('Bau die Suche.'), ASSISTANT, user(RESULT, reminder))), OWNER)
        other = {'role': 'system', 'content': '# Environment\nYou have been invoked in the following environment'}
        self.assertIsNone(classify(request(user('Bau die Suche.'), ASSISTANT, user(RESULT), other)))
        self.assertEqual(classify(request(user('Bau die Suche.'), other)), OWNER, 'a system message after the first prompt')

    def test_owner_wins_when_a_turn_mixes_both(self):
        self.assertEqual(classify(request(user(OTHER_AUTOMATIC[0], 'Und danach bitte deployen.'))), OWNER)


if __name__ == '__main__':
    unittest.main()
