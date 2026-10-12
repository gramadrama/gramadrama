#!/usr/bin/env python3
"""Model-only live checks and the correction shortcut in a private GNOME session.

Only synthetic text and the local stub server are used. No real desktop or model
configuration is read. Run with the system Python from the repository root.
"""
import os
import time

from integration_engine import (BIELIK_ENV, ENGINE, PANEL_EXTENSION, Run, Session,
                                Stub, check, log_leaks, panel_session, stream,
                                wait_panel, wait_until, write_settings)


def main():
    with Stub() as stub:
        def reply(request):
            content = request['messages'][-1]['content']
            fragment = content.split('<<<TEKST>>>\n', 1)[1].rsplit('\n<<<KONIEC TEKSTU>>>', 1)[0]
            return stream(fragment.replace('domm', 'dom').replace('testt', 'test'), pieces=10)

        stub.reply, stub.chunk_delay = reply, 0.15
        session = Session(sources=ENGINE, env=dict(BIELIK_ENV, LIVE_PAUSES='0.15,0.3'),
                          name='model-live', extensions=[PANEL_EXTENSION],
                          copy_logs_to=os.environ.get('GD_TEST_LOGS'))
        with session as s:
            write_settings(s, dict(stub.settings(), live_check=True, live_model_only=True,
                                   live_underlines=False))
            run = Run(s)
            wait_until(lambda: s.eval('typeof gdGramaDramaPanel') == 'object', 10,
                       what='panel extension enabled')
            win = run.gtk(4)
            time.sleep(2.2)  # let the engine refresh its switches
            for text, corrected, ready_first in [('To domm.', 'To dom.', True),
                                                  ('Nowy testt.', 'Nowy test.', False)]:
                mark = run.e.mark()
                run.gtk_field(win, 'tv', text, len(text))
                wait_panel(run, lambda state: state['live']['visible'] and state['live'].get('spinner'),
                           'checking indicator before the first result', timeout=10)
                if ready_first:
                    run.e.wait(lambda event: event['ev'] == 'live_model' and event.get('chars') == len(text),
                               mark, 10, 'completed background correction')
                    wait_panel(run, lambda state: state['live']['visible'] and not state['live'].get('spinner'),
                               'checking indicator stopped')
                panel_session(run, timeout=10)
                chats = [request for request in stub.chats()
                         if request['messages'][-1]['content'] == '<<<TEKST>>>\n' + text + '\n<<<KONIEC TEKSTU>>>']
                check(len(chats) == 1, 'the shortcut duplicated a live request')
                before_apply = run.e.mark()
                s.tap('ret')
                win.wait_text('tv', corrected)
                run.e.wait(lambda event: event['ev'] == 'verified' and event.get('outcome') == 'ok',
                           before_apply, 5, 'shared result applied safely')
                print('PASS model_live_' + ('cached' if ready_first else 'inflight'), flush=True)
            check(not any(path == '/v2/check' for path, _ in stub.requests),
                  'experimental corrections queried LanguageTool')
            check(not log_leaks(s.env['GRAMADRAMA_ENGINE_LOG'], ['domm', 'testt', '<<<TEKST>>>']),
                  'synthetic text leaked into the engine log')
            print('PASS model_live_privacy', flush=True)
        check(session.leftovers is not None and not any(session.leftovers.values())
              and not os.path.exists(session.work), 'isolated session cleanup failed')
        print('PASS model_live_teardown', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
