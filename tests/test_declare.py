from decimal import Decimal
from copy import deepcopy
import pytest
from fastapi.testclient import TestClient
from app import main
from app.engine import evaluate
from app.modules.declare.rules import normalize_basis, classify_segment
from app.errors import BusinessError

def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)

def new_run(c, dataset='clean'):
    return c.post('/api/runs', json={'dataset': dataset}).json()['manifest']['run_id']

SEGMENT = {'device': 'INV-A', 'start': '2026-10-15T02:00:00+00:00', 'end': '2026-10-15T02:10:00+00:00',
           'seconds': 600, 'event_ids': ['EA1'], 'baseline_kw': '100', 'actual_kw': '60',
           'loss_kwh': '6.666666666666666666666666667'}
EVENTS = {'EA1': {'id': 'EA1', 'device': 'INV-A', 'start': '2026-10-15T02:00:00+00:00', 'end': '2026-10-15T02:20:00+00:00'}}
WINDOW = {'id': 'PW-1', 'start': '2026-10-15T10:00:00+08:00', 'end': '2026-10-15T10:30:00+08:00', 'price_per_kwh': '-0.10'}
DEADLINE = '2026-10-15T18:00:00+08:00'

# 分类与裁定（管道级）

def test_clean_three_buckets_real(clean):
    s = clean['declaration']['summary']
    assert s['declared_kwh'] == '6.6667' and s['voluntary_kwh'] == '10.0000' and s['pending_kwh'] == '10.0000'
    assert s['split_balanced'] is True and s['known_loss_kwh'] == '26.6667'

def test_provisional_when_receipt_pending(clean):
    d = clean['declaration']
    assert d['ruling']['decision'] == '暂定申报'
    assert d['summary']['receipts_pending'] == ['ORD-EA2']
    assert d['ruling']['before_deadline'] is True

def test_dirty_unknown_segments_stay_pending(sample):
    d = sample['declaration']
    assert d['summary']['unknown_segments'] == 2 and d['summary']['pending_segments'] == 2
    assert d['ruling']['decision'] == '暂定申报'
    for row in d['rows']:
        if row['loss_kwh'] is None:
            assert row['bucket'] == '待查' and row['declared_kwh'] is None

def test_formal_when_receipts_complete():
    overrides = {'orders': [
        {'id': 'ORD-EA1', 'event_id': 'EA1', 'cap_kw': '80', 'receipt': '已回执'},
        {'id': 'ORD-EA2', 'event_id': 'EA2', 'cap_kw': '70', 'receipt': '已回执'},
        {'id': 'ORD-EB1', 'event_id': 'EB1', 'cap_kw': '40', 'receipt': '已回执'}]}
    d = evaluate({'dataset': 'clean', 'overrides': overrides})['declaration']
    assert d['ruling']['decision'] == '正式申报'
    s = d['summary']
    assert s['pending_kwh'] == '0.0000' and s['pending_segments'] == 0
    assert s['declared_kwh'] == '16.6667' and s['voluntary_kwh'] == '10.0000'
    assert s['split_balanced'] is True

def test_only_order_provable_part_declared(clean):
    # 值班想全报，复核只认指令上限可证明部分：10:00 片段申报(100-80)/6，其余归主动。
    row = next(r for r in clean['declaration']['rows'] if r['device'] == 'INV-A' and r['start'].endswith('02:00:00+00:00'))
    assert Decimal(row['declared_kwh']).quantize(Decimal('.0001')) == Decimal('3.3333')
    assert Decimal(row['voluntary_kwh']).quantize(Decimal('.0001')) == Decimal('3.3333')
    assert row['bucket'] == '拆分'

def test_no_order_inside_price_window_is_voluntary():
    basis = normalize_basis({'declare_deadline': DEADLINE, 'price_windows': [WINDOW], 'orders': []})
    r = classify_segment(dict(SEGMENT), basis, {})
    assert r['bucket'] == '主动' and Decimal(r['voluntary_kwh']) > 0 and r['pending_kwh'] == '0'
    assert 'NO_ORDER' in r['causes']

def test_no_order_outside_price_window_is_pending():
    basis = normalize_basis({'declare_deadline': DEADLINE})
    r = classify_segment(dict(SEGMENT), basis, {})
    assert r['bucket'] == '待查' and Decimal(r['pending_kwh']) > 0
    assert 'NO_ORDER' in r['causes'] and 'UNATTRIBUTED' in r['causes']

def test_remainder_outside_price_window_unattributed():
    basis = normalize_basis({'declare_deadline': DEADLINE,
                             'orders': [{'id': 'ORD-1', 'event_id': 'EA1', 'cap_kw': '80', 'receipt': '已回执'}]})
    r = classify_segment(dict(SEGMENT), basis, EVENTS)
    assert Decimal(r['declared_kwh']).quantize(Decimal('.0001')) == Decimal('3.3333')
    assert Decimal(r['pending_kwh']).quantize(Decimal('.0001')) == Decimal('3.3333')
    assert 'UNATTRIBUTED' in r['causes']

def test_cross_device_order_not_covering():
    # INV-B 的指令不能证明 INV-A 片段的责任。
    events = {'EB1': {'id': 'EB1', 'device': 'INV-B', 'start': '2026-10-15T02:00:00+00:00', 'end': '2026-10-15T02:20:00+00:00'}}
    basis = normalize_basis({'declare_deadline': DEADLINE,
                             'orders': [{'id': 'ORD-B', 'event_id': 'EB1', 'cap_kw': '40', 'receipt': '已回执'}]})
    r = classify_segment(dict(SEGMENT), basis, events)
    assert r['order_ids'] == [] and r['cap_kw'] is None

# 依据校验

def test_basis_rejects_nonnegative_price():
    with pytest.raises(BusinessError):
        normalize_basis({'declare_deadline': DEADLINE, 'price_windows': [dict(WINDOW, price_per_kwh='0.05')]})

def test_basis_rejects_overlapping_windows():
    other = dict(WINDOW, id='PW-2', start='2026-10-15T10:20:00+08:00', end='2026-10-15T10:40:00+08:00')
    with pytest.raises(BusinessError):
        normalize_basis({'declare_deadline': DEADLINE, 'price_windows': [WINDOW, other]})

def test_basis_rejects_bad_receipt():
    with pytest.raises(BusinessError):
        normalize_basis({'declare_deadline': DEADLINE,
                         'orders': [{'id': 'O', 'event_id': 'EA1', 'cap_kw': '80', 'receipt': '未知'}]})

def test_basis_rejects_unknown_event_strict():
    raw = {'declare_deadline': DEADLINE, 'orders': [{'id': 'O', 'event_id': 'NOPE', 'cap_kw': '80', 'receipt': '已回执'}]}
    with pytest.raises(BusinessError):
        normalize_basis(raw, {'EA1'}, strict_events=True)
    assert normalize_basis(raw, {'EA1'})['orders'][0]['event_id'] == 'NOPE'

def test_basis_requires_deadline():
    with pytest.raises(BusinessError):
        normalize_basis({'orders': []})

# 申报记录：形成、依据更新、回滚、幂等

def test_form_declaration_idempotent(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = new_run(c)
    a = c.post(f'/api/runs/{rid}/declare').json()
    b = c.post(f'/api/runs/{rid}/declare').json()
    assert a['created'] is True and b['created'] is False and a['id'] == b['id']
    got = c.get(f'/api/runs/{rid}/declare').json()
    assert got['versions'] == 1 and got['current']['result']['summary']['declared_kwh'] == '6.6667'

def test_declaration_missing_404(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    assert c.get(f'/api/runs/{new_run(c)}/declare').status_code == 404

def test_basis_update_success_keeps_history(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = new_run(c)
    first = c.post(f'/api/runs/{rid}/declare').json()
    basis = deepcopy(first['basis'])
    for order in basis['orders']:
        order['receipt'] = '已回执'
    updated = c.put(f'/api/runs/{rid}/declare/basis', json={'basis': basis}).json()
    assert updated['created'] is True and updated['id'] != first['id']
    assert updated['result']['ruling']['decision'] == '正式申报'
    got = c.get(f'/api/runs/{rid}/declare').json()
    assert got['versions'] == 2 and got['current']['id'] == updated['id']
    assert {h['id'] for h in got['history']} == {first['id'], updated['id']}

def test_basis_update_failure_restores_old_version(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = new_run(c)
    first = c.post(f'/api/runs/{rid}/declare').json()
    bad = deepcopy(first['basis'])
    bad['price_windows'][0]['price_per_kwh'] = '0.30'
    assert c.put(f'/api/runs/{rid}/declare/basis', json={'basis': bad}).status_code == 422
    got = c.get(f'/api/runs/{rid}/declare').json()
    assert got['current']['id'] == first['id'] and got['versions'] == 1

def test_basis_update_unknown_event_failure_keeps_old(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = new_run(c)
    first = c.post(f'/api/runs/{rid}/declare').json()
    bad = deepcopy(first['basis'])
    bad['orders'].append({'id': 'ORD-X', 'event_id': 'NOPE', 'cap_kw': '10', 'receipt': '已回执'})
    assert c.put(f'/api/runs/{rid}/declare/basis', json={'basis': bad}).status_code == 422
    assert c.get(f'/api/runs/{rid}/declare').json()['current']['id'] == first['id']

def test_same_basis_retry_no_new_declaration(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = new_run(c)
    first = c.post(f'/api/runs/{rid}/declare').json()
    again = c.put(f'/api/runs/{rid}/declare/basis', json={'basis': first['basis']}).json()
    assert again['created'] is False and again['id'] == first['id']
    got = c.get(f'/api/runs/{rid}/declare').json()
    assert got['versions'] == 1 and got['current']['basis'] == first['basis']

def test_form_conflict_after_basis_changed(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = new_run(c)
    first = c.post(f'/api/runs/{rid}/declare').json()
    basis = deepcopy(first['basis'])
    basis['orders'][0]['cap_kw'] = '75'
    assert c.put(f'/api/runs/{rid}/declare/basis', json={'basis': basis}).status_code == 200
    assert c.post(f'/api/runs/{rid}/declare').status_code == 409

def test_rollback_to_older_basis_reuses_version(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = new_run(c)
    first = c.post(f'/api/runs/{rid}/declare').json()
    basis = deepcopy(first['basis'])
    basis['orders'][0]['cap_kw'] = '75'
    c.put(f'/api/runs/{rid}/declare/basis', json={'basis': basis})
    back = c.put(f'/api/runs/{rid}/declare/basis', json={'basis': first['basis']}).json()
    assert back['created'] is False and back['id'] == first['id']
    assert c.get(f'/api/runs/{rid}/declare').json()['versions'] == 2

# 页面与入口

def test_declare_page_shows_buckets(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = new_run(c)
    html = c.get(f'/declare?run_id={rid}').text
    for text in ('申报损失', '主动降功率', '待查片段', '暂定申报', '负电价窗口', '强制指令与回执'):
        assert text in html

def test_declare_api_entry(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    view = c.post('/api/declare', json={}).json()['views']['declare']
    assert view['metrics']['裁定'] == '暂定申报'
    assert view['metrics']['申报损失_kWh'] == '6.6667'
