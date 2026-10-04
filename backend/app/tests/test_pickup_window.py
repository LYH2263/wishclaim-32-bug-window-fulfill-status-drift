"""取货窗卡窗一致性回归测试。

拍板规则：
- 认领（claim）永不卡窗；仅核销（fulfill）卡窗。
- 卡窗按服务端收到请求的时刻（app.main.now），换算到行内快照时区的本地墙钟。
- 每行可否核销只看该行发愿时固化的 pickup_window 快照；改默认窗不影响已发布行。
- 窗外核销 409 且零写入：行保持 claimed，墙/详情/已完成三处一致，无半核销。
"""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main as api_main
from app import seed

# 2026-10-05 是周一；UTC 12:00 = Asia/Shanghai 20:00（周一，闭窗时段）
FROZEN_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
CLOSED_WINDOW = {"weekdays": [0], "start_hour": 9, "end_hour": 18, "timezone": "Asia/Shanghai"}
OPEN_WINDOW = {"weekdays": [0], "start_hour": 19, "end_hour": 22, "timezone": "Asia/Shanghai"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(api_main, "now", lambda: FROZEN_NOW)
    with TestClient(api_main.app) as c:
        yield c


def create_wish(client, window):
    r = client.post("/api/wishes", json={"title": "t", "note": "n", "pickup_window": window})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def claim(client, wid, claimer="甲"):
    return client.post(f"/api/wishes/{wid}/claim", json={"claimer": claimer})


def find(rows, wid):
    return next(w for w in rows if w["id"] == wid)


def test_claim_never_gated_by_window(client):
    wid = create_wish(client, CLOSED_WINDOW)
    r = claim(client, wid)
    assert r.status_code == 200
    assert r.json()["status"] == "claimed"


def test_outside_fulfill_is_409_and_leaves_no_half_state(client):
    wid = create_wish(client, CLOSED_WINDOW)
    assert claim(client, wid).status_code == 200

    r = client.post(f"/api/wishes/{wid}/fulfill")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "outside_pickup_window"
    assert detail["state"]["is_open"] is False
    assert detail["window"]["weekdays"] == [0]
    assert "当前不在取货时间窗内" in detail["message"]
    assert "下次开窗" in detail["message"]

    # 详情仍为认领中
    d = client.get(f"/api/wishes/{wid}").json()
    assert d["status"] == "claimed"
    assert d["claimer"] == "甲"
    assert d["pickup_state"]["is_open"] is False

    # 墙卡仍为认领中、按钮应灰（is_open False）
    wall = find(client.get("/api/wishes").json(), wid)
    assert wall["status"] == "claimed"
    assert wall["pickup_state"]["is_open"] is False

    # 已完成页不得出现该行
    done = client.get("/api/done").json()
    assert all(w["id"] != wid for w in done)

    # 我的认领里仍在
    mine = client.get("/api/mine", params={"claimer": "甲"}).json()
    assert find(mine, wid)["status"] == "claimed"


def test_inside_fulfill_succeeds_and_appears_in_done(client):
    wid = create_wish(client, OPEN_WINDOW)
    assert claim(client, wid).status_code == 200
    r = client.post(f"/api/wishes/{wid}/fulfill")
    assert r.status_code == 200
    assert find(client.get("/api/done").json(), wid)["status"] == "fulfilled"


def test_changing_default_window_does_not_move_claimed_row(client):
    wid = create_wish(client, CLOSED_WINDOW)
    snapshot_before = client.get(f"/api/wishes/{wid}").json()["pickup_window"]
    assert claim(client, wid).status_code == 200

    # 默认窗改成此刻开放（全周全天），只影响此后发布
    r = client.put("/api/rules/pickup-window", json={
        "weekdays": [0, 1, 2, 3, 4, 5, 6], "start_hour": 0, "end_hour": 24,
        "timezone": "Asia/Shanghai"})
    assert r.status_code == 200

    # 已认领行仍按旧快照：墙、详情都判窗外
    wall = find(client.get("/api/wishes").json(), wid)
    assert wall["pickup_window"] == snapshot_before
    assert wall["pickup_state"]["is_open"] is False
    detail = client.get(f"/api/wishes/{wid}").json()
    assert detail["pickup_window"] == snapshot_before
    assert detail["pickup_state"]["is_open"] is False

    # 核销仍被旧快照挡住
    assert client.post(f"/api/wishes/{wid}/fulfill").status_code == 409
    assert client.get(f"/api/wishes/{wid}").json()["status"] == "claimed"

    # 新发布的愿望才吃新默认窗（此刻开放）
    new_id = client.post("/api/wishes", json={"title": "新", "note": ""}).json()["id"]
    assert client.get(f"/api/wishes/{new_id}").json()["pickup_state"]["is_open"] is True


def test_rule_change_plus_outside_fulfill_failure_returns_to_before(client):
    wid = create_wish(client, CLOSED_WINDOW)
    snapshot_before = json.dumps(
        client.get(f"/api/wishes/{wid}").json()["pickup_window"], sort_keys=True)
    assert claim(client, wid).status_code == 200

    client.put("/api/rules/pickup-window", json={
        "weekdays": [1, 2], "start_hour": 1, "end_hour": 2,
        "timezone": "Asia/Tokyo"})
    # 窗外核销失败
    assert client.post(f"/api/wishes/{wid}/fulfill").status_code == 409

    row = client.get(f"/api/wishes/{wid}").json()
    assert row["status"] == "claimed"
    assert json.dumps(row["pickup_window"], sort_keys=True) == snapshot_before
    wall = find(client.get("/api/wishes").json(), wid)
    assert wall["status"] == "claimed"
    assert wall["pickup_state"]["is_open"] is False


def test_legacy_row_without_snapshot_is_not_gated(client):
    # 旧数据：无快照行，认领与核销都不因窗外被挡
    seed.init_db()
    import sqlite3
    from app.db import db_path
    c = sqlite3.connect(db_path())
    cur = c.execute(
        "INSERT INTO wishes(title,note,status,data_quality,pickup_window) VALUES(?,?,?,?,?)",
        ("旧愿望", "", "open", "clean", None))
    c.commit()
    legacy_id = cur.lastrowid
    c.close()

    assert claim(client, legacy_id).status_code == 200
    r = client.post(f"/api/wishes/{legacy_id}/fulfill")
    assert r.status_code == 200
