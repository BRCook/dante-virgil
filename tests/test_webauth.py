from virgil.webauth import ATTEMPTS, SESSION_LIFETIME, Attempts, Sessions, check_password, hash_password


def test_password_hashing():
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$") and "correct horse" not in stored
    assert check_password("correct horse", stored)
    assert not check_password("wrong horse", stored)
    assert hash_password("correct horse") != stored  # salted
    assert not check_password("x", None)
    assert not check_password("x", "garbage")
    assert not check_password("x", "md5$1$2$3$4$5")


def test_sessions_expire():
    now = [0.0]
    sessions = Sessions(clock=lambda: now[0])
    token = sessions.create()
    assert sessions.valid(token)
    assert not sessions.valid("made-up") and not sessions.valid(None)
    now[0] += SESSION_LIFETIME + 1
    assert not sessions.valid(token)


def test_logging_out():
    sessions = Sessions()
    a, b = sessions.create(), sessions.create()
    sessions.end(a)
    assert not sessions.valid(a) and sessions.valid(b)
    sessions.end_all()
    assert not sessions.valid(b)


def test_guessing_is_slowed():
    now = [0.0]
    attempts = Attempts(clock=lambda: now[0])
    for _ in range(ATTEMPTS):
        assert not attempts.blocked("10.0.0.9")
        attempts.failed("10.0.0.9")
    assert attempts.blocked("10.0.0.9")
    assert not attempts.blocked("10.0.0.8")
    now[0] += 61
    assert not attempts.blocked("10.0.0.9")
