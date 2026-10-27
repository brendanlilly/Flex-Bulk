from webbsearch.retrieval.analyzer import compact_id, looks_like_part_number, normalize


def test_sizes_and_units():
    assert normalize('3/4" CxC 90° Ell') == ("3/4", "cxc", "90", "elbow")
    assert normalize("1 1/4in ball valve FPT") == ("1-1/4", "ball", "valve", "fip")
    assert normalize(".75 pex crimp tees") == ("3/4", "pex", "crimp", "tee")
    assert normalize("50gal NG water heater") == ("50", "gal", "natgas", "water", "heater")
    assert normalize("60k btu furnace") == normalize("60,000 BTU furnace")


def test_capacitor_and_amps():
    assert normalize("45/5MFD 440V cap") == ("45/5", "mfd", "440", "volt", "cap")
    assert normalize("40A 2-pole contactor")[:2] == ("40", "amp")


def test_dimensions():
    assert "16x25" in normalize("16x25x1 MERV 8 filter")


def test_part_numbers():
    assert compact_id("LF25AUB-Z3") == "LF25AUBZ3"
    assert looks_like_part_number("LF25AUB-Z3")
    assert looks_like_part_number("30001234")
    assert not looks_like_part_number("valve")
