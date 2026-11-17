from app import channels

SURVEY = """@@radio wl0 02:00:00:00:00:01
buflen=[4408] version=[109] count=[3]
[ 0] SSID[Neighbour One] BSSID[02:AA:00:00:00:01] channel[ 1] frequency[2412] numsta[-1] rssi[-50] noise[-84] beacon[100] cap[11] dtim[0] rate[11] enc[PSK2 ]
[ 1] SSID[odd]name[] BSSID[02:AA:00:00:00:02] channel[ 6] frequency[2437] numsta[-1] rssi[-80] noise[-84] beacon[100] cap[11] dtim[0] rate[11] enc[PSK2 ]
[ 2] SSID[HomeNet] BSSID[02:00:00:00:00:02] channel[11] frequency[2462] numsta[3] rssi[-60] noise[-84] beacon[100] cap[11] dtim[0] rate[11] enc[PSK2 ]
@@channels 1 2 3 4 5 6 7 8 9 10 11
@@radio wl1 02:00:00:00:00:11
buflen=[12] version=[109] count=[0]
@@channels 36 40 44 48 52 149
@@end
"""


def radio(key, bssid, channel, networks, band="2.4 GHz", width=20, allowed=()):
    return {"key": key, "bssid": bssid, "channel": channel, "width": width, "band": band,
            "networks": networks, "allowed": list(allowed)}


def test_survey_is_parsed_per_radio():
    s = channels.parse_survey(SURVEY)
    assert s["wl0"]["bssid"] == "02:00:00:00:00:01"
    assert [n["channel"] for n in s["wl0"]["networks"]] == [1, 6, 11]
    assert s["wl0"]["networks"][1] == {"ssid": "odd]name[", "bssid": "02:aa:00:00:00:02", "channel": 6, "rssi": -80}
    assert s["wl1"]["networks"] == [] and s["wl1"]["allowed"] == [36, 40, 44, 48, 52, 149]


def test_overlap_and_width():
    assert channels.overlap(1, 1) == 1 and channels.overlap(1, 6) == 0 and 0 < channels.overlap(1, 3) < 1
    assert channels.covers(44, 40) == {44, 48} and channels.covers(44, 80) == {36, 40, 44, 48}
    assert channels.overlap(36, 48, 80) == 1 and channels.overlap(36, 48, 20) == 0
    assert channels.candidates("5 GHz", 20, [36, 40, 44, 48, 52]) == [36, 40, 44, 48]


def test_our_own_routers_do_not_count_as_neighbours():
    nets = channels.parse_survey(SURVEY)["wl0"]["networks"]
    assert channels.interference(nets, 11, 20, {"02:00:00:00:00:02"}) == (0.0, 0)
    assert channels.interference(nets, 1, 20, set()) == (45.0, 1)


def test_plan_avoids_the_loud_neighbour_and_separates_our_routers():
    nets = channels.parse_survey(SURVEY)["wl0"]["networks"]
    a = radio("a", "02:00:00:00:00:01", 1, nets)
    b = radio("b", "02:00:00:00:00:02", 1, [])
    result = channels.plan([a, b])
    assert result["a"]["recommended"] != 1
    assert result["a"]["recommended"] != result["b"]["recommended"]


def test_plan_without_any_survey_keeps_current_channels_when_already_apart():
    radios = [radio("a", "", 44, [], "5 GHz"), radio("b", "", 149, [], "5 GHz")]
    result = channels.plan(radios)
    assert (result["a"]["recommended"], result["b"]["recommended"]) == (44, 149)


def test_a_quiet_radar_channel_in_use_is_kept():
    result = channels.plan([radio("a", "", 60, [], "5 GHz")])
    assert result["a"]["recommended"] == 60
