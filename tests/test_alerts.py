import logging

from tradeloop.alerts import AlertHandler, Notifier


class Sink:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def telegram(self, token, chat, text):
        if self.fail:
            raise OSError("telegram down")
        self.sent.append(("telegram", text))

    def gmail(self, user, pw, to, subject, text):
        self.sent.append(("gmail", text))


def make(tmp_path, sink, *, tg=True, gm=True, now=[1_000_000], **kw):
    return Notifier(telegram=("tok", "1") if tg else None, gmail=("u", "p", "to") if gm else None,
                    state_file=tmp_path / "state.json", clock_ms=lambda: now[0],
                    telegram_sender=sink.telegram, gmail_sender=sink.gmail, **kw), now


def send(n, text, **kw):
    t = n.notify(text, **kw)
    n.flush()
    return t


def test_plain_alert_goes_to_telegram_only(tmp_path):
    sink = Sink()
    n, _ = make(tmp_path, sink)
    send(n, "watchdog: stale data")
    assert [c for c, _ in sink.sent] == ["telegram"]
    assert "stale data" in sink.sent[0][1]


def test_critical_alert_also_emails(tmp_path):
    sink = Sink()
    n, _ = make(tmp_path, sink)
    send(n, "kill switch: flattening", critical=True)
    assert sorted(c for c, _ in sink.sent) == ["gmail", "telegram"]


def test_gmail_is_the_backup_when_telegram_fails(tmp_path):
    sink = Sink(fail=True)
    n, _ = make(tmp_path, sink)
    send(n, "reconciliation mismatch")
    assert [c for c, _ in sink.sent] == ["gmail"]


def test_gmail_alone_works_and_no_channel_does_not_crash(tmp_path):
    sink = Sink()
    n, _ = make(tmp_path, sink, tg=False)
    send(n, "x")
    assert [c for c, _ in sink.sent] == ["gmail"]
    n, _ = make(tmp_path / "other", Sink(), tg=False, gm=False)
    send(n, "nobody to tell")


def test_repeats_are_held_back_then_reminded_with_a_count(tmp_path):
    sink = Sink()
    n, now = make(tmp_path, sink, dedupe_seconds=300)
    send(n, "stale data: no tick for 343s")
    now[0] += 5_000
    assert send(n, "stale data: no tick for 348s") is None       # same alert, different number
    now[0] += 5_000
    assert send(n, "stale data: no tick for 353s") is None
    now[0] += 301_000
    send(n, "stale data: no tick for 700s")
    assert len(sink.sent) == 2
    assert "repeated 2x" in sink.sent[1][1]


def test_dedupe_state_survives_a_new_process(tmp_path):
    sink = Sink()
    n, now = make(tmp_path, sink)
    send(n, "heartbeat lost (70s old)")
    n2, _ = make(tmp_path, sink, now=now)                         # cron starts a fresh process each minute
    assert send(n2, "heartbeat lost (130s old)") is None
    assert len(sink.sent) == 1


def test_a_flood_is_cut_off(tmp_path):
    sink = Sink()
    n, _ = make(tmp_path, sink, max_per_minute=3)
    for i in range(10):
        send(n, f"different problem number {'x' * i} word{i}a")
    assert len(sink.sent) == 3


def test_broken_senders_never_raise(tmp_path):
    class Boom(Sink):
        def telegram(self, *a): raise RuntimeError("boom")
        def gmail(self, *a): raise RuntimeError("boom")
    n, _ = make(tmp_path, Boom())
    send(n, "anything", critical=True)                            # no exception is the assertion
    sink = Sink()
    n2 = Notifier(telegram=("t", "1"), state_file=tmp_path / "no" / "dir" / "\0bad", clock_ms=lambda: 10_000_000,
                  telegram_sender=sink.telegram)
    send(n2, "bad state path")                                    # unwritable state file: logged, alert still sent
    assert len(sink.sent) == 1


def test_handler_forwards_only_alert_lines(tmp_path):
    sink = Sink()
    n, _ = make(tmp_path, sink)
    logger = logging.getLogger("alert_test")
    logger.propagate = False
    logger.addHandler(AlertHandler(n))
    logger.warning("ALERT kill switch (watchdog): flattening s1")
    logger.error("ALERT reconciliation mismatch, x")
    logger.error("ordinary error")
    logger.info("ALERT but only info")
    n.flush()
    texts = [t for _, t in sink.sent]
    assert any("flattening s1" in t for t in texts) and any("mismatch" in t for t in texts)
    assert not any("ordinary" in t or "only info" in t for t in texts)
    kinds = [c for c, _ in sink.sent]
    assert kinds.count("gmail") == 2                              # kill switch and ERROR level are critical


def test_token_is_not_logged_when_send_fails(tmp_path, caplog):
    import urllib.error

    class Leaky(Sink):
        def telegram(self, token, chat, text):
            raise urllib.error.URLError("https://api.telegram.org/botSECRET/sendMessage")
    n, _ = make(tmp_path, Leaky(), gm=False)
    with caplog.at_level(logging.ERROR):
        send(n, "x")
    assert "SECRET" not in caplog.text
