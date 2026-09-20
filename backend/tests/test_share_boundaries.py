"""分享链接边界与并发测试。

覆盖：
- 零次/恰好用完时公开详情与创建者列表状态一致
- 并发/重复提交取件不会超过 max_downloads（TOCTOU 回归）
- 过期临界（跨天/恰好到期）不会误计数
- 未设上限/永久有效链接长期可用
- 非法参数（0、非数字）在创建时被拒绝
"""
import io
import time
import threading

import pytest


def _upload_file(client, name='boundary.txt', content=b'boundary content'):
    resp = client.post(
        '/api/upload',
        data={'file': (io.BytesIO(content), name)},
        content_type='multipart/form-data'
    )
    return resp.get_json()['file_id']


def _create_share(client, token, file_id, **kwargs):
    payload = {'file_id': file_id}
    payload.update(kwargs)
    resp = client.post(
        '/api/share',
        json=payload,
        headers={'Authorization': f'Bearer {token}'}
    )
    return resp


def test_create_share_zero_downloads_rejected(client, auth_token):
    """最大下载次数为 0 必须在创建时拒绝，不能生成永远无法取件的链接"""
    file_id = _upload_file(client)
    resp = _create_share(client, auth_token, file_id, max_downloads=0)
    assert resp.status_code == 400


def test_create_share_zero_expire_rejected(client, auth_token):
    """有效期为 0 必须拒绝（防止生成即过期链接）"""
    file_id = _upload_file(client)
    resp = _create_share(client, auth_token, file_id, expire_hours=0)
    assert resp.status_code == 400


@pytest.mark.parametrize('bad_value', ['abc', {}, []])
def test_create_share_invalid_numbers_rejected(client, auth_token, bad_value):
    """非数字的有效期/次数必须拒绝"""
    file_id = _upload_file(client)
    resp = _create_share(client, auth_token, file_id, max_downloads=bad_value)
    assert resp.status_code == 400


def test_last_remaining_download_state_consistency(client, auth_token):
    """剩余 1 次时：详情和列表都允许取件；取完后两处都显示已用完且数据一致"""
    file_id = _upload_file(client)
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=24, max_downloads=1
    ).get_json()['share_id']

    # 取件前：公开详情与创建者列表都判定为有效、剩余 1
    detail_before = client.get(f'/api/share/{share_id}').get_json()
    listed_before = [
        s for s in client.get(
            '/api/shares', headers={'Authorization': f'Bearer {auth_token}'}
        ).get_json() if s['share_id'] == share_id
    ][0]
    assert detail_before['is_valid'] is True
    assert detail_before['remaining_downloads'] == 1
    assert listed_before['is_valid'] is True
    assert listed_before['remaining_downloads'] == 1

    # 唯一一次取件必须成功
    dl = client.get(f'/api/share/{share_id}/download')
    assert dl.status_code == 200

    # 取完后：详情与列表同时失效、错误信息一致、计数都是 1
    detail_after = client.get(f'/api/share/{share_id}').get_json()
    listed_after = [
        s for s in client.get(
            '/api/shares', headers={'Authorization': f'Bearer {auth_token}'}
        ).get_json() if s['share_id'] == share_id
    ][0]
    assert detail_after['is_valid'] is False
    assert detail_after['error_msg'] == '分享链接下载次数已用完'
    assert detail_after['remaining_downloads'] == 0
    assert detail_after['download_count'] == 1
    assert listed_after['is_valid'] is False
    assert listed_after['error_msg'] == detail_after['error_msg']
    assert listed_after['download_count'] == 1
    assert listed_after['remaining_downloads'] == 0

    # 再取必须失败，且计数不能再增加
    dl_again = client.get(f'/api/share/{share_id}/download')
    assert dl_again.status_code == 404
    assert '下载次数已用完' in dl_again.get_json()['error']
    assert client.get(f'/api/share/{share_id}').get_json()['download_count'] == 1


def test_concurrent_downloads_never_exceed_limit(client, auth_token):
    """并发取件成功次数必须恰好等于上限，计数不得超限"""
    from app import app

    file_id = _upload_file(client)
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=24, max_downloads=5
    ).get_json()['share_id']

    statuses = []
    lock = threading.Lock()

    def hit():
        with app.test_client() as c:
            resp = c.get(f'/api/share/{share_id}/download')
        with lock:
            statuses.append(resp.status_code)

    threads = [threading.Thread(target=hit) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(200) == 5
    assert statuses.count(404) == 20

    count = client.get(f'/api/share/{share_id}').get_json()
    assert count['download_count'] == 5
    assert count['is_valid'] is False
    assert count['remaining_downloads'] == 0


def test_duplicate_submit_does_not_double_count(client, auth_token):
    """同一次取件的快速重复提交（上限 1）只能成功一次"""
    from app import app

    file_id = _upload_file(client)
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=24, max_downloads=1
    ).get_json()['share_id']

    statuses = []
    lock = threading.Lock()

    def hit():
        with app.test_client() as c:
            resp = c.get(f'/api/share/{share_id}/download')
        with lock:
            statuses.append(resp.status_code)

    threads = [threading.Thread(target=hit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(statuses) == [200, 404]
    assert client.get(f'/api/share/{share_id}').get_json()['download_count'] == 1


def test_expired_share_not_downloadable_even_with_remaining(client, auth_token, db_conn):
    """已过期但仍有剩余次数：不能取件、不能计数；详情和列表状态一致"""
    file_id = _upload_file(client)
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=1, max_downloads=5
    ).get_json()['share_id']

    # 构造一个“跨天”的过期时间点：过期于 1 天前
    cursor = db_conn.cursor()
    cursor.execute(
        'UPDATE share_links SET expires_at = ? WHERE id = ?',
        (time.time() - 86400, share_id)
    )
    db_conn.commit()

    resp = client.get(f'/api/share/{share_id}/download')
    assert resp.status_code == 404
    assert '已过期' in resp.get_json()['error']

    detail = client.get(f'/api/share/{share_id}').get_json()
    listed = [
        s for s in client.get(
            '/api/shares', headers={'Authorization': f'Bearer {auth_token}'}
        ).get_json() if s['share_id'] == share_id
    ][0]
    assert detail['is_valid'] is False
    assert detail['error_msg'] == '分享链接已过期'
    assert listed['is_valid'] is False
    assert listed['error_msg'] == '分享链接已过期'
    # 过期拒绝不能误计数
    assert detail['download_count'] == 0
    assert listed['download_count'] == 0
    assert detail['remaining_downloads'] == 5


def test_exact_expiry_boundary(client, auth_token, db_conn):
    """恰好到期（expires_at == now）判定为过期，临界不错误放行"""
    file_id = _upload_file(client)
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=1, max_downloads=5
    ).get_json()['share_id']

    cursor = db_conn.cursor()
    cursor.execute(
        'UPDATE share_links SET expires_at = ? WHERE id = ?',
        (time.time(), share_id)
    )
    db_conn.commit()

    assert client.get(f'/api/share/{share_id}').get_json()['is_valid'] is False
    assert client.get(f'/api/share/{share_id}/download').status_code == 404


def test_unlimited_share_stays_valid_long_term(client, auth_token):
    """未设上限（永久+无限次）的链接：大量取件后仍有效，计数准确"""
    file_id = _upload_file(client)
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=-1, max_downloads=-1
    ).get_json()['share_id']

    for _ in range(30):
        assert client.get(f'/api/share/{share_id}/download').status_code == 200

    info = client.get(f'/api/share/{share_id}').get_json()
    assert info['download_count'] == 30
    assert info['max_downloads'] is None
    assert info['expires_at'] is None
    assert info['remaining_downloads'] is None
    assert info['is_valid'] is True


def test_expired_then_limit_exhausted_reports_expired_first(client, auth_token, db_conn):
    """既过期又用完时，错误原因稳定为“已过期”（两处一致）"""
    file_id = _upload_file(client)
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=1, max_downloads=1
    ).get_json()['share_id']

    cursor = db_conn.cursor()
    cursor.execute(
        'UPDATE share_links SET expires_at = ?, download_count = 1 WHERE id = ?',
        (time.time() - 10, share_id)
    )
    db_conn.commit()

    detail = client.get(f'/api/share/{share_id}').get_json()
    assert detail['is_valid'] is False
    assert detail['error_msg'] == '分享链接已过期'

    dl = client.get(f'/api/share/{share_id}/download')
    assert dl.status_code == 404
    assert '已过期' in dl.get_json()['error']
