from app import firmware

YEAR = '''<html><body><h1>Index of /betas/2026/</h1><hr><pre><a href="../">../</a>
<a href="01-05-2026-r60000/">01-05-2026-r60000/</a>                                 05-Jan-2026 06:49                   -
<a href="11-13-2026-r66631/">11-13-2026-r66631/</a>                                 13-Nov-2026 07:23                   -
<a href="11-10-2026-r66557/">11-10-2026-r66557/</a>                                 10-Nov-2026 06:49                   -
<a href="notes.txt">notes.txt</a>                                          10-Nov-2026 06:49                 120
</pre><hr></body></html>'''
FOLDER = '''<pre><a href="../">../</a>
<a href="factory-to-ddwrt.img">factory-to-ddwrt.img</a>                               13-Nov-2026 07:27            18540670
<a href="r6220-webflash.bin">r6220-webflash.bin</a>                                 13-Nov-2026 07:27            33296412
<a href="readme.txt">readme.txt</a>                                         13-Nov-2026 07:27                 100
</pre>'''
FOLDERS = ["asus-rt-ac66u", "asus-rt-ac68u", "netgear-r6220", "netgear-r6250", "trendnet-818DRU", "trendnet-812DRUv2"]


def test_builds_are_parsed_and_ordered():
    builds = firmware.parse_builds(YEAR, 2026)
    assert [b["build"] for b in builds] == [60000, 66557, 66631]
    assert builds[-1] == {"build": 66631, "date": "2026-11-13", "path": "2026/11-13-2026-r66631"}


def test_listing_sizes():
    assert dict(firmware.parse_listing(FOLDER))["r6220-webflash.bin"] == 33296412
    assert dict(firmware.parse_listing(YEAR))["11-13-2026-r66631/"] is None


def test_folder_suggestions_for_real_model_names():
    assert firmware.suggest_folder("Trendnet TEW818DRU", FOLDERS) == "trendnet-818DRU"
    assert firmware.suggest_folder("Asus RT-AC66U", FOLDERS) == "asus-rt-ac66u"
    assert firmware.suggest_folder("Netgear R6220", FOLDERS) == "netgear-r6220"
    assert firmware.suggest_folder("Acme Unknown 9000", FOLDERS) is None
    assert firmware.suggest_folder(None, FOLDERS) is None


def test_file_suggestion_prefers_webflash_and_never_factory():
    assert firmware.suggest_file([("factory-to-ddwrt.img", 1), ("r6220-webflash.bin", 2)]) == "r6220-webflash.bin"
    assert firmware.suggest_file([("dd-wrt-66631-Asus_RT-AC66U.trx", 1)]) == "dd-wrt-66631-Asus_RT-AC66U.trx"
    assert firmware.suggest_file([("factory-to-ddwrt.img", 1)]) is None
    assert firmware.suggest_file([("a.bin", 1), ("b.bin", 1)]) is None


def test_pattern_follows_the_build_number():
    pattern = firmware.to_pattern("dd-wrt-66631-Asus_RT-AC66U.trx", 66631)
    assert pattern == "dd-wrt-{build}-Asus_RT-AC66U.trx"
    assert firmware.to_pattern("r6220-webflash.bin", 66631) == "r6220-webflash.bin"


INDEX = [
    {"build": 53323, "date": "2023-07-15", "path": "2023/07-15-2023-r53323"},
    {"build": 53562, "date": "2023-08-20", "path": "2023/08-20-2023-r53562"},
    {"build": 53733, "date": "2023-09-12", "path": "2023/09-12-2023-r53733"},
    {"build": 54652, "date": "2023-12-30", "path": "2023/12-30-2023-r54652"},
    {"build": 66631, "date": "2026-11-13", "path": "2026/11-13-2026-r66631"},
]


def test_running_date_prefers_the_site_then_the_banner():
    assert firmware.release_date({"release": "07/15/23"}) == "2023-07-15"
    assert firmware.release_date({"release": None}) is None
    assert firmware.running_date({"build": 53562, "release": "08/21/23"}, INDEX) == "2023-08-20"
    assert firmware.running_date({"build": 50000, "release": "01/02/23"}, INDEX) == "2023-01-02"


def test_a_step_inside_the_limit_has_no_warning():
    judged = firmware.assess(53323, "2023-07-15", INDEX[2], limit=62)
    assert judged == {"gap_days": 59, "blocked": None, "warnings": []}


def test_a_long_jump_warns_and_the_same_build_is_blocked():
    judged = firmware.assess(53323, "2023-07-15", INDEX[4], limit=62)
    assert judged["gap_days"] == 1217 and not judged["blocked"] and len(judged["warnings"]) == 1
    assert firmware.assess(53323, "2023-07-15", INDEX[0], limit=62)["blocked"]


def test_downgrade_and_unknown_age_warn():
    assert "older" in firmware.assess(54652, "2023-12-30", INDEX[0], limit=62)["warnings"][0]
    assert "unknown" in firmware.assess(53323, None, INDEX[1], limit=62)["warnings"][0]


def test_suggested_step_is_the_newest_inside_the_limit():
    assert firmware.suggest_next(53323, "2023-07-15", INDEX, limit=62)["build"] == 53733
    # Nothing inside the limit: offer the smallest step instead of the latest build.
    assert firmware.suggest_next(54652, "2023-12-30", INDEX, limit=62)["build"] == 66631
    assert firmware.suggest_next(53733, "2023-09-12", INDEX, limit=62)["build"] == 54652
    assert firmware.suggest_next(66631, "2026-11-13", INDEX, limit=62) is None


MTD = '''write=yes
dev:    size   erasesize  name
mtd0: 00100000 00020000 "u-boot"
mtd2: 02000000 00020000 "linux"
mtd3: 01c00000 00020000 "ubi"
MemFree:           80924 kB
MemAvailable:      93668 kB
'''


def test_flash_facts_and_limits():
    facts = firmware.parse_flash_facts(MTD)
    assert facts == {"partition": 0x2000000, "mem_avail": 93668 * 1024, "has_write": True}
    assert firmware.flash_problems(facts, 28_000_000) == []
    assert "partition" in firmware.flash_problems(facts, 34_000_000)[0]
    low = facts | {"mem_avail": 40 * 1048576}
    assert "memory" in firmware.flash_problems(low, 28_000_000)[0]
    assert len(firmware.flash_problems(firmware.parse_flash_facts(""), 1)) == 3


def test_builds_that_failed_on_a_model_warn_and_are_not_suggested():
    failed = [{"model": "Netgear R6220", "build": 54652, "from": 53733, "at": 0}]
    assert firmware.failure_warning("Netgear R6220", 53733, failed) is None
    assert "did not boot" in firmware.failure_warning("Netgear R6220", 54652, failed)
    assert "newer" in firmware.failure_warning("Netgear R6220", 66631, failed)
    assert firmware.failure_warning("Netgear R6220", 66631, []) is None
    assert firmware.suggest_next(53733, "2023-09-12", INDEX, limit=62, below=54652) is None
    assert firmware.suggest_next(53323, "2023-07-15", INDEX, limit=62, below=53733)["build"] == 53562


STEPS = [{"build": n, "date": f"2024-01-{d:02d}"} for d, n in enumerate(range(100, 111), start=1)]


def test_bisect_picks_the_midpoint_between_good_and_failed():
    assert firmware.bisect(100, 110, STEPS)["build"] == 105  # 9 between: 101..109
    assert firmware.bisect(105, 110, STEPS)["build"] == 107  # 106..109
    assert firmware.bisect(107, 110, STEPS)["build"] == 108  # 108, 109
    assert firmware.bisect(108, 110, STEPS)["build"] == 109  # one left
    assert firmware.bisect(109, 110, STEPS) is None


def test_last_good_uses_running_successes_and_failed_starts():
    failed = [{"model": "M", "build": 110, "from": 103}, {"model": "M", "build": 112, "from": 111}]
    assert firmware.last_good(101, failed, [], 110) == 103
    assert firmware.last_good(101, failed, [{"model": "M", "build": 105}], 110) == 105
    assert firmware.last_good(106, failed, [], 110) == 106
    assert firmware.last_good(None, [], [], 110) is None


def test_recommend_after_a_failure(monkeypatch):
    failed = [{"model": "M", "build": 110, "from": 100, "at": 0}]
    monkeypatch.setattr(firmware, "failures", lambda model=None: failed)
    monkeypatch.setattr(firmware, "successes", lambda model=None: [])
    on_good = firmware.recommend(100, "2024-01-01", "M", STEPS, limit=62)
    assert on_good["next"]["build"] == 105 and on_good["bisecting"] and on_good["good"] == 100
    # A router recovered to an older build steps back up to the last good one first.
    failed[0]["from"] = 105
    below = firmware.recommend(102, "2024-01-03", "M", STEPS, limit=62)
    assert below["next"]["build"] == 105 and not below["bisecting"]
    failed[0]["from"] = 109
    done = firmware.recommend(109, "2024-01-10", "M", STEPS, limit=62)
    assert done["next"] is None and not done["bisecting"] and done["end_of_life"]
    monkeypatch.setattr(firmware, "failures", lambda model=None: [])
    assert firmware.recommend(100, "2024-01-01", "M", STEPS, limit=62)["next"]["build"] == 110


def test_a_build_above_the_failure_ends_end_of_life(monkeypatch):
    steps = STEPS + [{"build": 111, "date": "2024-01-12"}, {"build": 112, "date": "2024-01-13"}]
    failed = [{"model": "M", "build": 110, "from": 109, "at": 0}]
    monkeypatch.setattr(firmware, "failures", lambda model=None: failed)
    monkeypatch.setattr(firmware, "successes", lambda model=None: [])
    assert firmware.recommend(109, "2024-01-10", "M", steps, limit=62)["end_of_life"]
    # r111 entered by hand, installed and booted: the failure is superseded.
    monkeypatch.setattr(firmware, "successes", lambda model=None: [{"model": "M", "build": 111, "at": 0}])
    after = firmware.recommend(111, "2024-01-12", "M", steps, limit=62)
    assert not after["end_of_life"] and after["first_bad"] is None and after["next"]["build"] == 112
    # A sibling router still on r105 steps forward, skipping the failed r110.
    sibling = firmware.recommend(105, "2024-01-06", "M", steps, limit=62)
    assert sibling["next"]["build"] == 112
    assert "did not boot" in firmware.failure_warning("M", 110, failed, 105, [{"build": 111}])
    assert firmware.failure_warning("M", 112, failed, 105, [{"build": 111}]) is None
    assert "newer" in firmware.failure_warning("M", 112, failed, 105, [])
