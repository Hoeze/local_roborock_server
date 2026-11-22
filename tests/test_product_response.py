import json
from pathlib import Path

import pytest

from roborock_local_server.backend import (
    ServerContext,
    default_endpoint_rules,
    resolve_route,
)


class _PairedRuntimeCredentials:
    """Treat every inventory device as paired so home_payload keeps it."""

    def resolve_device(self, *, duid: str = "", **_kwargs):
        return {"did": f"did-{duid}", "device_mqtt_usr": "mqtt-user"}

    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


def _make_context(tmp_path: Path, region: str = "us") -> ServerContext:
    return ServerContext(
        api_host="api.example.com",
        mqtt_host="mqtt.example.com",
        wood_host="wood.example.com",
        region=region,
        protocol_login_email="user@example.com",
        localkey="key123",
        duid="duid-1",
        mqtt_usr="usr",
        mqtt_passwd="pwd",
        mqtt_clientid="cid",
        https_port=443,
        mqtt_tls_port=8883,
        http_jsonl=tmp_path / "http.jsonl",
        mqtt_jsonl=tmp_path / "mqtt.jsonl",
        loggers={},
        runtime_credentials=_PairedRuntimeCredentials(),
    )


def _products_v5(
    tmp_path: Path, devices: list[dict[str, object]], **shared_devices: list[dict[str, object]]
) -> dict:
    # The keyword names the inventory list of the shared devices, like receivedDevices=[...].
    inventory = {"home": {"id": 424242, "name": "Test Home"}, "devices": devices, **shared_devices}
    (tmp_path / "web_api_inventory.json").write_text(json.dumps(inventory) + "\n", encoding="utf-8")
    route, payload = resolve_route(
        rules=default_endpoint_rules(),
        context=_make_context(tmp_path),
        clean_path="/api/v5/product",
        query_params={},
        body_params={},
        method="GET",
    )
    assert route == "get_products_v5"
    return payload["data"]


def _plugin_agreements(tmp_path: Path) -> list[dict]:
    _route, payload = resolve_route(
        rules=default_endpoint_rules(),
        context=_make_context(tmp_path),
        clean_path="/api/v1/checkAppAgreement",
        query_params={},
        body_params={},
        method="GET",
    )
    return payload["data"]["userPluginAgreementList"]


def _versions_by_type(agreements: list[dict]) -> dict[str, int]:
    return {agreement["type"]: agreement["version"] for agreement in agreements}


_DEVICE = {
    "duid": "device-a",
    "name": "Vacuum A",
    "model": "roborock.vacuum.a15",
    "product_id": "iot-product-a",
}


def test_product_entry_carries_rriotid_category_id_and_agreements(tmp_path: Path) -> None:
    data = _products_v5(tmp_path, [_DEVICE])

    [category_detail] = data["categoryDetailList"]
    category = category_detail["category"]
    [product] = category_detail["productList"]

    # Product.rriotid is what the app joins the home data device's productId on.
    assert product["rriotid"] == "iot-product-a"
    assert product["categoryId"] == category["id"]
    assert [agreement["type"] for agreement in product["agreements"]] == [
        "USER_AGREEMENT",
        "PRIVACY_POLICY",
    ]


def test_product_agreements_are_signed_in_plugin_agreements(tmp_path: Path) -> None:
    device = dict(_DEVICE, model="roborock.vacuum.a307")
    data = _products_v5(tmp_path, [device])
    plugin_agreements = _plugin_agreements(tmp_path)

    [product] = data["categoryDetailList"][0]["productList"]
    latest = _versions_by_type(product["agreements"])
    signed = _versions_by_type(
        [agreement for agreement in plugin_agreements if agreement["deviceId"] == "device-a"]
    )

    # hasSignLastestPluginPrivacyAgreement in the device plugin returns true
    # only if both lists name both types and every signed version is at least
    # the latest version. Otherwise the plugin goes to its privacy dialog path.
    for agreement_type in ("USER_AGREEMENT", "PRIVACY_POLICY"):
        assert signed[agreement_type] >= latest[agreement_type]


def test_product_category_carries_code(tmp_path: Path) -> None:
    data = _products_v5(tmp_path, [_DEVICE])

    category = data["categoryDetailList"][0]["category"]

    assert category["code"] == "robot.vacuum.cleaner"
    assert category["displayName"] == "robot.vacuum.cleaner"


def test_product_rriotid_matches_home_data_device_product_id(tmp_path: Path) -> None:
    from roborock_local_server.bundled_backend.https_server.routes.user.homes.service import home_payload

    data = _products_v5(tmp_path, [_DEVICE])
    home = home_payload(_make_context(tmp_path))

    device_product_ids = {device["productId"] for device in home["devices"]}
    rriotids = {
        product["rriotid"]
        for category_detail in data["categoryDetailList"]
        for product in category_detail["productList"]
    }
    assert device_product_ids == rriotids


def test_product_picture_fields_use_app_json_names(tmp_path: Path) -> None:
    device = dict(_DEVICE, iconUrl="https://images.example.com/vacuum.png")

    data = _products_v5(tmp_path, [device])

    [product] = data["categoryDetailList"][0]["productList"]
    # Product$$serializer only knows the lower-case names picurl/cardpicurl.
    assert product["picurl"] == "https://images.example.com/vacuum.png"
    assert product["cardpicurl"] == "https://images.example.com/vacuum.png"
    assert "cardPicUrl" not in product


def test_string_product_id_uses_cloud_numeric_id_by_model(tmp_path: Path) -> None:
    # The cloud lists newer models (e.g. a226) under a string product id in home
    # data. The numeric id the app requests plugins with comes from the cloud's
    # /api/v5/product, which the import stores in the snapshot.
    snapshot = {
        "web_api_cache": {
            "plugin_catalogs": {
                "product": {
                    "data": {
                        "categoryDetailList": [
                            {
                                "category": {"id": 1, "code": "robot.vacuum.cleaner"},
                                "productList": [{"id": 199, "model": "roborock.vacuum.a226"}],
                            }
                        ]
                    }
                }
            }
        }
    }
    (tmp_path / "web_api_inventory_full_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
    device = dict(_DEVICE, model="roborock.vacuum.a226", product_id="30uADinyLKa3Qpga60B1Ke")

    data = _products_v5(tmp_path, [device])

    [product] = data["categoryDetailList"][0]["productList"]
    assert product["id"] == 199
    assert product["rriotid"] == "30uADinyLKa3Qpga60B1Ke"


def _labels(data_point: dict) -> dict[int, str]:
    return {code: entry["desc"]["en"] for entry in data_point["value"] for code in entry["value"]}


def test_product_entry_enables_scene_tiles(tmp_path: Path) -> None:
    # No cloud snapshot: the built-in fallback values.
    data = _products_v5(tmp_path, [_DEVICE])

    category_detail = data["categoryDetailList"][0]
    [product] = category_detail["productList"]
    category = category_detail["category"]

    # ProductEntity.sceneProduct() looks up exactly this tag; values as the cloud sends them.
    assert {"name": "scene_2.0", "requirePlugin": True, "pluginLevel": 3000, "forceShow": True} in product[
        "productTags"
    ]
    # Like the cloud: lower-case "cardspec" on the category, a JSON string
    # holding the spec in a "data" wrapper (decoded by the app as CardSpecV2Data).
    assert "cardSpec" not in category
    assert isinstance(category["cardspec"], str)
    wrapper = json.loads(category["cardspec"])
    assert set(wrapper) == {"data"}
    card_spec = wrapper["data"]
    assert set(card_spec) == {"state", "extra", "battery", "offline", "offpeak"}
    assert card_spec["state"]["dps"] == 121
    assert card_spec["battery"] == {"dps": 122}
    assert card_spec["extra"] == {"dps": 128}
    assert card_spec["offline"]["dps"] == 135
    assert card_spec["offpeak"] == {"dps": 133}
    for data_point in (card_spec["state"], card_spec["offline"]):
        for entry in data_point["value"]:
            assert set(entry) == {"desc", "value"}
            assert entry["desc"]["en"]
            assert all(isinstance(code, int) for code in entry["value"])


# Decoded cardspec of the robot.vacuum.cleaner category in the EU cloud's
# GET /api/v5/product answer, fetched 2026-11-21 by @Hoeze (account without
# devices; all 56 vacuum models share it). Posted in PR #208 and kept unchanged.
CLOUD_VACUUM_CARD_SPEC_EU = json.loads(
    (Path(__file__).parent / "fixtures" / "cloud_vacuum_cardspec_eu.json").read_text(encoding="utf-8")
)

# Deliberate differences of the built-in fallback from the cloud spec. The app's
# bundled template (assets/cardSpec/robot.vacuum.cleaner.json, 4.60.06) maps
# these extra codes; they only add labels for codes the cloud leaves unmapped.
_TEMPLATE_ONLY_STATE_LABELS = {
    13: "Washing the mop",
    33: "Reinstalling mop cloth mounts",
    34: "Detaching mop cloth mounts",
    36: "Demo Mode",
    37: "Demo Mode",
    38: "Housework in progress",
    39: "Remote Control",
    40: "Emergency Stop",
    101: "Positioning",
    102: "Cleaning",
    103: "Cleaning",
    104: "Cleaning",
    108: "Charge Pending",
}
# The template also maps the offline codes 2, 3, 4 to the labels of 34, 51, 103.
_TEMPLATE_ONLY_OFFLINE_LABELS = {
    2: "Sleep & Powered Off",
    3: "Low Battery & Powered Off",
    4: "Updating & Restarting",
}


def _en_labels(data_point: dict) -> dict[int, str]:
    # The cloud's English label for offline code 1 has a trailing space
    # ("Powered Off Manually "); the fallback drops it.
    return {code: entry["desc"]["en"].strip() for entry in data_point["value"] for code in entry["value"]}


def test_fallback_vacuum_card_spec_matches_cloud_fixture(tmp_path: Path) -> None:
    data = _products_v5(tmp_path, [_DEVICE])

    ours = json.loads(data["categoryDetailList"][0]["category"]["cardspec"])
    cloud = CLOUD_VACUUM_CARD_SPEC_EU
    assert set(ours) == set(cloud) == {"data"}
    ours, cloud = ours["data"], cloud["data"]

    # Same data points with the same dps.
    assert set(ours) == set(cloud)
    for name in cloud:
        assert ours[name]["dps"] == cloud[name]["dps"], name
    for name in ("extra", "battery", "offpeak"):
        assert ours[name] == cloud[name]
    for name in ("state", "offline"):
        assert ours[name]["desc"]["en"] == cloud[name]["desc"]["en"]

    # Every code the cloud maps has the cloud's English label; the only extra
    # codes are the documented template ones. Only English is built in; the
    # cloud's 22 languages come from the snapshot when there is one.
    cloud_states, our_states = _en_labels(cloud["state"]), _en_labels(ours["state"])
    assert {code: our_states.get(code) for code in cloud_states} == cloud_states
    assert {code: label for code, label in our_states.items() if code not in cloud_states} == (
        _TEMPLATE_ONLY_STATE_LABELS
    )
    cloud_offline, our_offline = _en_labels(cloud["offline"]), _en_labels(ours["offline"])
    assert {code: our_offline.get(code) for code in cloud_offline} == cloud_offline
    assert {code: label for code, label in our_offline.items() if code not in cloud_offline} == (
        _TEMPLATE_ONLY_OFFLINE_LABELS
    )


def test_cloud_fixture_card_spec_is_passed_through_with_all_languages(tmp_path: Path) -> None:
    cloud_card_spec = json.dumps(CLOUD_VACUUM_CARD_SPEC_EU, ensure_ascii=False)
    _write_cloud_product_snapshot(
        tmp_path,
        [
            {
                "category": {"id": 1, "code": "robot.vacuum.cleaner", "cardspec": cloud_card_spec},
                "productList": [{"id": 101, "model": "roborock.vacuum.a15", "productTags": []}],
            }
        ],
    )

    data = _products_v5(tmp_path, [_DEVICE])

    category = data["categoryDetailList"][0]["category"]
    assert json.loads(category["cardspec"]) == CLOUD_VACUUM_CARD_SPEC_EU
    languages = {lang for entry in json.loads(category["cardspec"])["data"]["state"]["value"] for lang in entry["desc"]}
    assert len(languages) == 22


def test_card_spec_only_on_known_categories(tmp_path: Path) -> None:
    other = dict(
        _DEVICE,
        duid="device-b",
        name="Other",
        model="roborock.other.b1",
        product_id="iot-product-b",
        category="other.category",
    )

    data = _products_v5(tmp_path, [_DEVICE, other])

    categories = {entry["category"]["code"]: entry["category"] for entry in data["categoryDetailList"]}
    assert set(categories) == {"robot.vacuum.cleaner", "other.category"}
    assert isinstance(categories["robot.vacuum.cleaner"]["cardspec"], str)
    assert "cardspec" not in categories["other.category"]
    assert "cardSpec" not in categories["other.category"]


def test_unknown_category_has_no_card_spec(tmp_path: Path) -> None:
    device = dict(_DEVICE, category="other.category")

    data = _products_v5(tmp_path, [device])

    category = data["categoryDetailList"][0]["category"]
    assert category["displayName"] == "other.category"
    assert "cardSpec" not in category
    assert "cardspec" not in category


def _write_cloud_product_snapshot(tmp_path: Path, category_details: list[dict]) -> None:
    snapshot = {"web_api_cache": {"plugin_catalogs": {"product": {"data": {"categoryDetailList": category_details}}}}}
    (tmp_path / "web_api_inventory_full_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")


# Shape of the cloud's /api/v5/product answer: cardspec is a JSON string with
# the spec in {"data": ...}, the labels in many languages.
_CLOUD_VACUUM_SPEC = {
    "state": {
        "dps": 121,
        "desc": {"en": "State", "de": "Status", "fr": "État"},
        "value": [{"desc": {"en": "Charging", "de": "Wird geladen", "fr": "En charge"}, "value": [8]}],
    },
    "battery": {"dps": 122},
}
_CLOUD_VACUUM_CARD_SPEC = json.dumps({"data": _CLOUD_VACUUM_SPEC}, ensure_ascii=False)
_CLOUD_SCENE_TAG = {"name": "scene_2.0", "requirePlugin": False, "pluginLevel": 1234, "forceShow": True}
_CLOUD_OTHER_TAG = {"name": "other_tag", "requirePlugin": False, "pluginLevel": 0, "forceShow": False}


def test_cloud_snapshot_product_tags_and_card_spec_win(tmp_path: Path) -> None:
    _write_cloud_product_snapshot(
        tmp_path,
        [
            {
                "category": {"id": 7, "code": "robot.vacuum.cleaner", "cardspec": _CLOUD_VACUUM_CARD_SPEC},
                "productList": [
                    {"id": 101, "model": "roborock.vacuum.a15", "productTags": [_CLOUD_SCENE_TAG, _CLOUD_OTHER_TAG]}
                ],
            }
        ],
    )

    data = _products_v5(tmp_path, [_DEVICE])

    category_detail = data["categoryDetailList"][0]
    [product] = category_detail["productList"]
    # The cloud's tags are passed through unchanged, not the built-in values.
    assert product["productTags"] == [_CLOUD_SCENE_TAG, _CLOUD_OTHER_TAG]
    # The cloud's card spec string is passed through as is, all languages included.
    card_spec = category_detail["category"]["cardspec"]
    assert card_spec == _CLOUD_VACUUM_CARD_SPEC
    charging = json.loads(card_spec)["data"]["state"]["value"][0]["desc"]
    assert charging == {"en": "Charging", "de": "Wird geladen", "fr": "En charge"}


def test_cloud_snapshot_without_scene_tag_is_respected(tmp_path: Path) -> None:
    # The cloud lists the model but sends no scene_2.0 tag: none is added.
    _write_cloud_product_snapshot(
        tmp_path,
        [
            {
                "category": {"id": 7, "code": "robot.vacuum.cleaner", "cardspec": _CLOUD_VACUUM_CARD_SPEC},
                "productList": [{"id": 101, "model": "roborock.vacuum.a15", "productTags": []}],
            }
        ],
    )

    data = _products_v5(tmp_path, [_DEVICE])

    [product] = data["categoryDetailList"][0]["productList"]
    assert product["productTags"] == []


def test_model_missing_from_cloud_snapshot_uses_fallback(tmp_path: Path) -> None:
    # Snapshot without an entry for this model and with an empty category
    # cardspec (some bundles default it to ''): built-in values as fallback.
    _write_cloud_product_snapshot(
        tmp_path,
        [
            {
                "category": {"id": 7, "code": "robot.vacuum.cleaner", "cardspec": ""},
                "productList": [{"id": 199, "model": "roborock.vacuum.a226", "productTags": [_CLOUD_OTHER_TAG]}],
            }
        ],
    )

    data = _products_v5(tmp_path, [_DEVICE])

    category_detail = data["categoryDetailList"][0]
    [product] = category_detail["productList"]
    assert product["productTags"] == [
        {"name": "scene_2.0", "requirePlugin": True, "pluginLevel": 3000, "forceShow": True}
    ]
    card_spec = json.loads(category_detail["category"]["cardspec"])["data"]
    assert card_spec["state"]["dps"] == 121
    assert _labels(card_spec["state"])[8] == "Charging"


_MOWER = dict(
    _DEVICE,
    duid="device-m",
    name="Mower",
    model="roborock.mower.m1",
    product_id="iot-product-m",
    category="roborock.mower",
)


def test_non_vacuum_category_gets_no_fallback_scene_tag(tmp_path: Path) -> None:
    data = _products_v5(tmp_path, [_DEVICE, _MOWER])

    by_category = {entry["category"]["code"]: entry for entry in data["categoryDetailList"]}
    [vacuum] = by_category["robot.vacuum.cleaner"]["productList"]
    [mower] = by_category["roborock.mower"]["productList"]
    assert [tag["name"] for tag in vacuum["productTags"]] == ["scene_2.0"]
    assert mower["productTags"] == []
    assert "cardspec" not in by_category["roborock.mower"]["category"]


def test_non_vacuum_category_takes_scene_tag_and_card_spec_from_cloud(tmp_path: Path) -> None:
    mower_spec = json.dumps({"data": {"state": {"dps": 201, "desc": {"en": "State"}, "value": []}}})
    _write_cloud_product_snapshot(
        tmp_path,
        [
            {
                "category": {"id": 9, "code": "roborock.mower", "cardspec": mower_spec},
                "productList": [{"id": 301, "model": "roborock.mower.m1", "productTags": [_CLOUD_SCENE_TAG]}],
            }
        ],
    )

    data = _products_v5(tmp_path, [_MOWER])

    category_detail = data["categoryDetailList"][0]
    [mower] = category_detail["productList"]
    assert mower["productTags"] == [_CLOUD_SCENE_TAG]
    assert category_detail["category"]["cardspec"] == mower_spec


@pytest.mark.parametrize(
    ("model", "rriotid", "expected_id"),
    [
        # The rriotids are the ones in product_catalog.json, which onboarding stores.
        ("roborock.vacuum.a307", "4J89YHjAc2uUG9W0HLxKPP", 263),
        ("roborock.vacuum.a87", "5gUei3OIJIXVD3eD85Balg", 103),
        ("roborock.vacuum.a15", "1YYW18rpgyAJTISwb1NM91", 23),
        ("roborock.vacuum.sc05", "5ayEx3aKgStqZZ0v5IpMBP", 10001),
        ("roborock.wdv.a194", "MUKZV1YH7JkqlXXjJnO64", 180),
    ],
)
def test_product_uses_built_in_cloud_product_id(tmp_path: Path, model: str, rriotid: str, expected_id: int) -> None:
    device = dict(_DEVICE, model=model, product_id=rriotid)

    data = _products_v5(tmp_path, [device])

    [product] = data["categoryDetailList"][0]["productList"]
    # The app requests the device plugin with this id.
    assert product["id"] == expected_id
    assert product["rriotid"] == rriotid


def test_product_of_unknown_model_keeps_hashed_id(tmp_path: Path) -> None:
    from shared.data_helpers import stable_int

    device = dict(_DEVICE, model="roborock.vacuum.zz99", product_id="iot-product-z")

    data = _products_v5(tmp_path, [device])

    [product] = data["categoryDetailList"][0]["productList"]
    assert product["id"] == stable_int("iot-product-z") % 1_000_000


def test_numeric_inventory_product_id_wins_over_built_in_id(tmp_path: Path) -> None:
    device = dict(_DEVICE, model="roborock.vacuum.a307", product_id="4242")

    data = _products_v5(tmp_path, [device])

    [product] = data["categoryDetailList"][0]["productList"]
    assert product["id"] == 4242


def _write_cloud_products(
    tmp_path: Path,
    products: dict[str, int],
    app_plugin_records: list[dict[str, object]] | None = None,
    *,
    base_url: str | None = None,
) -> None:
    # The import stores the cloud's /api/v5/product answer in the snapshot, and
    # the /api/v1/appplugin answer for the account's product ids.
    product_list = [{"id": product_id, "model": model} for model, product_id in products.items()]
    catalogs: dict[str, object] = {
        "product": {"data": {"categoryDetailList": [{"category": {"id": 1}, "productList": product_list}]}}
    }
    if app_plugin_records is not None:
        catalogs["appplugin"] = {"data": app_plugin_records}
    snapshot: dict[str, object] = {"web_api_cache": {"plugin_catalogs": catalogs}}
    if base_url is not None:
        snapshot["meta"] = {"base_url": base_url}
    (tmp_path / "web_api_inventory_full_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")


@pytest.mark.parametrize(
    ("model", "rriotid", "import_id"),
    [
        # The built-in ids come from the EU cloud: 185 for the a199 and 103 for the a87.
        ("roborock.vacuum.a199", "3PWf17rt73QjM3F1DaZelu", 199),
        ("roborock.vacuum.a87", "5gUei3OIJIXVD3eD85Balg", 110),
        # A hardcoded 110 for the a87 used to win over the import.
        ("roborock.vacuum.a87", "5gUei3OIJIXVD3eD85Balg", 103),
        # The import also wins over a numeric id in home data.
        ("roborock.vacuum.a307", "4242", 263),
    ],
)
def test_cloud_import_product_id_wins_over_built_in_id(
    tmp_path: Path, model: str, rriotid: str, import_id: int
) -> None:
    # Product ids differ by cloud region, so the account's own import decides.
    _write_cloud_products(tmp_path, {model: import_id})
    device = dict(_DEVICE, model=model, product_id=rriotid)

    data = _products_v5(tmp_path, [device])

    [product] = data["categoryDetailList"][0]["productList"]
    assert product["id"] == import_id


def test_model_missing_from_cloud_import_gets_built_in_id_and_record(tmp_path: Path) -> None:
    _write_cloud_products(tmp_path, {"roborock.vacuum.a226": 199}, base_url="https://euiot.roborock.com")
    device = dict(_DEVICE, model="roborock.vacuum.a307", product_id="4J89YHjAc2uUG9W0HLxKPP")

    data = _products_v5(tmp_path, [device])

    [product] = data["categoryDetailList"][0]["productList"]
    assert product["id"] == 263
    assert _plugin_sources(tmp_path, 263) == [(263, _built_in_source("roborock.vacuum.a307"))]


def test_app_gets_a307_plugin_without_cloud_import(tmp_path: Path) -> None:
    import urllib.parse

    from https_server.routes.plugin.common import APPPLUGIN_LIST

    device = dict(_DEVICE, model="roborock.vacuum.a307", product_id="4J89YHjAc2uUG9W0HLxKPP")
    [product] = _products_v5(tmp_path, [device])["categoryDetailList"][0]["productList"]
    # The app requests its device plugin with the product id from /api/v5/product.
    body = {"apilevel": 10046, "productids": [product["id"]], "type": 2}
    route, payload = resolve_route(
        rules=default_endpoint_rules(),
        context=_make_context(tmp_path),
        clean_path="/api/v1/appplugin",
        query_params={},
        body_params={"__json": [json.dumps(body)]},
        method="POST",
    )

    assert product["id"] == 263
    assert route == "download_code"
    [record] = payload["data"]
    [a307_record] = [item for item in APPPLUGIN_LIST if item["productid"] == 263]
    assert record["productid"] == 263
    assert record["version"] == a307_record["version"]
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(record["url"]).query)["src"] == [a307_record["url"]]


def _app_plugin_records(tmp_path: Path, product_id: int) -> list[dict]:
    # The app requests its device plugin with the product id from /api/v5/product.
    body = {"apilevel": 10046, "productids": [product_id], "type": 2}
    route, payload = resolve_route(
        rules=default_endpoint_rules(),
        context=_make_context(tmp_path),
        clean_path="/api/v1/appplugin",
        query_params={},
        body_params={"__json": [json.dumps(body)]},
        method="POST",
    )
    assert route == "download_code"
    return payload["data"]


def _v5_product_list(tmp_path: Path, device: dict[str, object]) -> list[dict]:
    return _products_v5(tmp_path, [device])["categoryDetailList"][0]["productList"]


def _legacy_product_list(tmp_path: Path, device: dict[str, object]) -> list[dict]:
    from https_server.endpoint_rules import _build_product_response

    home_data = {
        "devices": [{"duid": device["duid"], "productId": device["product_id"]}],
        "products": [{"id": device["product_id"], "model": device["model"], "category": "robot.vacuum.cleaner"}],
    }
    return _build_product_response(_make_context(tmp_path), home_data)["categoryDetailList"][0]["productList"]


def _product_ids_by_model(data: dict) -> dict[str, int]:
    return {
        product["model"]: product["id"] for category in data["categoryDetailList"] for product in category["productList"]
    }


def _built_in_source(model: str) -> str:
    from shared.device_plugin_catalog import catalog_for_region

    return catalog_for_region("eu").records_by_model[model]["url"]


def _plugin_sources(tmp_path: Path, product_id: int) -> list[tuple[int, str]]:
    import urllib.parse

    return [
        (record["productid"], urllib.parse.parse_qs(urllib.parse.urlsplit(record["url"]).query)["src"][0])
        for record in _app_plugin_records(tmp_path, product_id)
    ]


_A87 = dict(_DEVICE, model="roborock.vacuum.a87", product_id="5gUei3OIJIXVD3eD85Balg")
_A199 = dict(_DEVICE, model="roborock.vacuum.a199", product_id="3PWf17rt73QjM3F1DaZelu")
_A226 = dict(_DEVICE, duid="device-b", model="roborock.vacuum.a226", product_id="30uADinyLKa3Qpga60B1Ke")
_WASHER = dict(_DEVICE, duid="device-b", model="roborock.wm.a91", product_id="rriot-wm-a91")


@pytest.mark.parametrize("product_list", [_v5_product_list, _legacy_product_list], ids=["api_v5", "legacy"])
def test_built_in_id_that_the_import_gives_another_model_is_skipped(tmp_path: Path, product_list) -> None:
    from shared.data_helpers import stable_int

    # The import gives 199 to the a199, as the US cloud does, and lacks the
    # a226. The built-in id of the a226 comes from the EU cloud, and it is 199.
    _write_cloud_products(tmp_path, {"roborock.vacuum.a199": 199})

    [product] = product_list(tmp_path, _A226)

    assert product["id"] == stable_int(_A226["product_id"]) % 1_000_000


def test_devices_after_a_us_import_get_the_built_in_record_of_their_model(tmp_path: Path) -> None:
    from shared.data_helpers import stable_int

    _write_cloud_products(tmp_path, {"roborock.vacuum.a199": 199}, base_url="https://usiot.roborock.com")
    hashed_id = stable_int(_A226["product_id"]) % 1_000_000

    product_ids = _product_ids_by_model(_products_v5(tmp_path, [_A199, _A226]))

    assert product_ids == {"roborock.vacuum.a199": 199, "roborock.vacuum.a226": hashed_id}
    assert _plugin_sources(tmp_path, hashed_id) == [(hashed_id, _built_in_source("roborock.vacuum.a226"))]
    # The snapshot has no record for 199, and in the EU cloud 199 is the a226.
    assert _plugin_sources(tmp_path, 199) == [(199, _built_in_source("roborock.vacuum.a199"))]


def test_snapshot_record_wins_over_the_built_in_record_of_the_model(tmp_path: Path) -> None:
    a199_record = {"version": 1, "url": "https://app-files.roborock.com/iot/plugin/a199.zip", "productid": 199}
    _write_cloud_products(tmp_path, {"roborock.vacuum.a199": 199}, [a199_record])
    _products_v5(tmp_path, [_A199, _A226])

    assert _plugin_sources(tmp_path, 199) == [(199, a199_record["url"])]


def test_a87_with_the_us_import_id_110_gets_the_a87_record(tmp_path: Path) -> None:
    # In the EU cloud, and so in the EU catalog, 110 is the roborock.wm.a91 washer.
    _write_cloud_products(tmp_path, {"roborock.vacuum.a87": 110}, base_url="https://usiot.roborock.com")
    [product] = _v5_product_list(tmp_path, _A87)

    assert product["id"] == 110
    assert _plugin_sources(tmp_path, 110) == [(110, _built_in_source("roborock.vacuum.a87"))]


def test_a87_with_the_numeric_inventory_id_110_gets_the_a87_record(tmp_path: Path) -> None:
    from shared.device_plugin_catalog import catalog_for_region

    # An older import stored the US id 110 of the a87 in home data. There is
    # no snapshot.
    [product] = _v5_product_list(tmp_path, dict(_A87, product_id="110"))

    assert product["id"] == 110
    assert _plugin_sources(tmp_path, 110) == [(110, _built_in_source("roborock.vacuum.a87"))]
    # The answer is a copy, so the built-in record keeps its own id.
    assert catalog_for_region("eu").records_by_model["roborock.vacuum.a87"]["productid"] == 103


def test_built_in_id_that_another_model_holds_in_home_data_is_skipped(tmp_path: Path) -> None:
    from shared.data_helpers import stable_int

    # The a87 holds 110 in home data, which is the built-in id of the washer.
    hashed_id = stable_int(_WASHER["product_id"]) % 1_000_000

    product_ids = _product_ids_by_model(_products_v5(tmp_path, [dict(_A87, product_id="110"), _WASHER]))

    assert product_ids == {"roborock.vacuum.a87": 110, "roborock.wm.a91": hashed_id}
    assert _plugin_sources(tmp_path, 110) == [(110, _built_in_source("roborock.vacuum.a87"))]
    assert _plugin_sources(tmp_path, hashed_id) == [(hashed_id, _built_in_source("roborock.wm.a91"))]


def test_app_gets_no_built_in_plugin_for_an_id_of_two_models(tmp_path: Path) -> None:
    # The import gives 110 to the washer, and the a87 keeps 110 in home data.
    # The app requests 110 for both, so the server cannot tell them apart.
    _write_cloud_products(tmp_path, {"roborock.wm.a91": 110})

    product_ids = _product_ids_by_model(_products_v5(tmp_path, [dict(_A87, product_id="110"), _WASHER]))

    assert product_ids == {"roborock.vacuum.a87": 110, "roborock.wm.a91": 110}
    assert _app_plugin_records(tmp_path, 110) == []


def test_app_gets_no_built_in_plugin_for_an_id_without_inventory_device(tmp_path: Path) -> None:
    # The app requests the ids that /api/v5/product answers. No device has 263.
    device = dict(_DEVICE, model="roborock.vacuum.a15", product_id="1YYW18rpgyAJTISwb1NM91")
    [product] = _v5_product_list(tmp_path, device)

    assert product["id"] == 23
    assert _app_plugin_records(tmp_path, 263) == []


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({"meta": {"base_url": "https://usiot.roborock.com"}, "user_data": {"region": "eu"}}, "us"),
        ({"meta": {"base_url": "https://euiot.roborock.com"}}, "eu"),
        ({"meta": {"base_url": None}, "user_data": {"region": "cn"}}, "cn"),
        ({"user_data": {"rriot": {"r": {"r": "EU"}}}}, "eu"),
        # An import of an unknown region, and no import at all, take network.region.
        ({"meta": {"base_url": "https://example.com"}}, "ru"),
        (None, "ru"),
    ],
    ids=["base_url_wins", "base_url", "user_data_region", "rriot_region", "unknown_import_region", "no_import"],
)
def test_server_cloud_region(tmp_path: Path, snapshot: dict | None, expected: str) -> None:
    from https_server.routes.plugin.common import server_cloud_region

    if snapshot is not None:
        (tmp_path / "web_api_inventory_full_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    assert server_cloud_region(_make_context(tmp_path, region="ru")) == expected


_US_A307_ZIP = "https://rrpkg-us.roborock.com/iot/plugin/us-a307.zip"


def test_us_server_takes_ids_and_records_from_the_us_catalog(tmp_path: Path, monkeypatch) -> None:
    from shared import device_plugin_catalog

    us_catalog = device_plugin_catalog.catalog_from_data(
        "us",
        {
            "products": {"roborock.vacuum.a307": {"id": 9263}},
            "appplugin": [{"version": 1, "url": _US_A307_ZIP, "productid": 9263}],
        },
    )
    monkeypatch.setitem(device_plugin_catalog.CATALOGS, "us", us_catalog)
    # Without an import, network.region decides, and it is us here.
    device = dict(_DEVICE, model="roborock.vacuum.a307", product_id="4J89YHjAc2uUG9W0HLxKPP")

    [product] = _v5_product_list(tmp_path, device)

    assert product["id"] == 9263
    assert _plugin_sources(tmp_path, 9263) == [(9263, _US_A307_ZIP)]


def test_us_import_without_us_catalog_gets_the_eu_catalog_by_model(tmp_path: Path) -> None:
    from shared import device_plugin_catalog

    assert "us" not in device_plugin_catalog.CATALOGS
    # The US import lacks the a307.
    _write_cloud_products(tmp_path, {"roborock.vacuum.a199": 199}, base_url="https://usiot.roborock.com")
    device = dict(_DEVICE, model="roborock.vacuum.a307", product_id="4J89YHjAc2uUG9W0HLxKPP")

    [product] = _v5_product_list(tmp_path, device)

    assert product["id"] == 263
    assert _plugin_sources(tmp_path, 263) == [(263, _built_in_source("roborock.vacuum.a307"))]


def _region_zip(region: str, model: str) -> str:
    return f"https://rrpkg-{region}.roborock.com/iot/plugin/{region}-{model}.zip"


def _add_region_catalog(monkeypatch, region: str, product_ids: dict[str, int], without_record: tuple = ()) -> None:
    from shared import device_plugin_catalog

    catalog = device_plugin_catalog.catalog_from_data(
        region,
        {
            "products": {model: {"id": product_id} for model, product_id in product_ids.items()},
            "appplugin": [
                {"version": 1, "url": _region_zip(region, model), "productid": product_id}
                for model, product_id in product_ids.items()
                if model not in without_record
            ],
        },
    )
    monkeypatch.setitem(device_plugin_catalog.CATALOGS, region, catalog)


def test_model_missing_from_the_region_catalog_gets_the_eu_record(tmp_path: Path, monkeypatch) -> None:
    # Without an import, network.region decides, and it is us here.
    _add_region_catalog(monkeypatch, "us", {"roborock.vacuum.a307": 9263})

    [product] = _v5_product_list(tmp_path, _A87)

    assert product["id"] == 103
    assert _plugin_sources(tmp_path, 103) == [(103, _built_in_source("roborock.vacuum.a87"))]


def test_model_missing_from_the_region_and_eu_catalogs_gets_the_record_of_another_region(
    tmp_path: Path, monkeypatch
) -> None:
    # After the region and EU, the other catalogs follow by region name.
    model = "roborock.vacuum.x9"
    _add_region_catalog(monkeypatch, "us", {"roborock.vacuum.a307": 9263})
    _add_region_catalog(monkeypatch, "ru", {model: 7009})
    _add_region_catalog(monkeypatch, "cn", {model: 8009})
    device = dict(_DEVICE, model=model, product_id="rriot-x9")

    [product] = _v5_product_list(tmp_path, device)

    assert product["id"] == 8009
    assert _plugin_sources(tmp_path, 8009) == [(8009, _region_zip("cn", model))]


def test_record_of_another_region_wins_over_an_id_without_record(tmp_path: Path, monkeypatch) -> None:
    _add_region_catalog(monkeypatch, "us", {"roborock.vacuum.a87": 9087}, without_record=("roborock.vacuum.a87",))

    [product] = _v5_product_list(tmp_path, _A87)

    assert product["id"] == 103
    assert _plugin_sources(tmp_path, 103) == [(103, _built_in_source("roborock.vacuum.a87"))]


def test_id_that_the_region_catalog_gives_another_model_is_skipped(tmp_path: Path, monkeypatch) -> None:
    # In the US cloud, 199 is the a199. The US catalog lacks the a226, whose EU
    # id is also 199.
    _add_region_catalog(monkeypatch, "us", {"roborock.vacuum.a199": 199})

    products = _products_v5(tmp_path, [_A199, _A226])["categoryDetailList"][0]["productList"]
    product_ids = {product["model"]: product["id"] for product in products}

    hashed_id = product_ids["roborock.vacuum.a226"]
    assert product_ids["roborock.vacuum.a199"] == 199
    assert hashed_id != 199
    assert _plugin_sources(tmp_path, 199) == [(199, _region_zip("us", "roborock.vacuum.a199"))]
    assert _plugin_sources(tmp_path, hashed_id) == [(hashed_id, _built_in_source("roborock.vacuum.a226"))]


_A307 = dict(_DEVICE, model="roborock.vacuum.a307", product_id="4J89YHjAc2uUG9W0HLxKPP")


def test_two_devices_of_one_model_get_one_product_and_its_record(tmp_path: Path) -> None:
    devices = [_A307, dict(_A307, duid="device-b", name="Vacuum B")]

    [product] = _products_v5(tmp_path, devices)["categoryDetailList"][0]["productList"]

    assert (product["id"], product["rriotid"]) == (263, _A307["product_id"])
    # Both devices get 263, but 263 names one model.
    assert _plugin_sources(tmp_path, 263) == [(263, _built_in_source("roborock.vacuum.a307"))]


@pytest.mark.parametrize("numeric_id", ["263", "4242"], ids=["built_in_id", "other_id"])
def test_devices_of_one_model_with_two_home_data_ids_get_two_products(tmp_path: Path, numeric_id: str) -> None:
    # One device has the rriotid in home data, the other a numeric id. The app
    # finds the product of a device by rriotid, so each home data id gets its
    # own product. Each of the product ids names only the a307.
    devices = [_A307, dict(_A307, duid="device-b", product_id=numeric_id)]

    products = _products_v5(tmp_path, devices)["categoryDetailList"][0]["productList"]

    assert [(product["rriotid"], product["id"]) for product in products] == [
        (_A307["product_id"], 263),
        (numeric_id, int(numeric_id)),
    ]
    for product_id in {263, int(numeric_id)}:
        assert _plugin_sources(tmp_path, product_id) == [(product_id, _built_in_source("roborock.vacuum.a307"))]


@pytest.mark.parametrize("inventory_key", ["receivedDevices", "received_devices"])
def test_shared_device_gets_the_built_in_id_and_record(tmp_path: Path, inventory_key: str) -> None:
    # The a307 is shared with the account, and the account's own device is the a15.
    shared_a307 = dict(_A307, duid="device-b", name="Shared Vacuum")

    data = _products_v5(tmp_path, [_DEVICE], **{inventory_key: [shared_a307]})

    assert _product_ids_by_model(data) == {"roborock.vacuum.a15": 23, "roborock.vacuum.a307": 263}
    assert _plugin_sources(tmp_path, 263) == [(263, _built_in_source("roborock.vacuum.a307"))]


# The import stores a catalog request that failed in this shape, without "data".
_PRODUCT_CATALOG_ERROR = {
    "path": "/api/v5/product",
    "method": "GET",
    "params": {},
    "json": {},
    "error": "Cloud request failed for /api/v5/product: {'code': 500}",
}
_APP_PLUGIN_CATALOG_ERROR = {
    "path": "/api/v1/appplugin",
    "method": "POST",
    "json": {"type": 2, "apilevel": 10043, "productids": []},
    "error": "no product ids available",
}


@pytest.mark.parametrize(
    "catalogs",
    [
        {"product": _PRODUCT_CATALOG_ERROR},
        {"appplugin": _APP_PLUGIN_CATALOG_ERROR},
        {
            "product": _PRODUCT_CATALOG_ERROR,
            "appplugin": _APP_PLUGIN_CATALOG_ERROR,
            "appplugin_ios": dict(_APP_PLUGIN_CATALOG_ERROR, json={"type": 1, "apilevel": 10043, "productids": []}),
        },
    ],
    ids=["product", "appplugin", "all"],
)
def test_snapshot_catalog_error_gets_the_built_in_id_and_record(tmp_path: Path, catalogs: dict) -> None:
    snapshot = {"web_api_cache": {"plugin_catalogs": catalogs}}
    (tmp_path / "web_api_inventory_full_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    [product] = _v5_product_list(tmp_path, _A307)

    assert product["id"] == 263
    assert _plugin_sources(tmp_path, 263) == [(263, _built_in_source("roborock.vacuum.a307"))]
