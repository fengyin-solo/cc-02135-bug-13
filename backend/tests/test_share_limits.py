"""分享链接有效期与下载次数的边界、并发和状态一致性测试"""
import io
import threading
import time
import urllib.request
import urllib.error
from socketserver import ThreadingMixIn
from wsgiref.simple_server import make_server, WSGIServer, WSGIRequestHandler

import pytest


def _upload_file(client, name='share_edge.txt', content=b'edge content'):
    data = {'file': (io.BytesIO(content), name)}
    resp = client.post('/api/upload', data=data, content_type='multipart/form-data')
    return resp.get_json()['file_id']


def _create_share(client, token, file_id, **kwargs):
    resp = client.post(
        '/api/share',
        json={'file_id': file_id, **kwargs},
        headers={'Authorization': f'Bearer {token}'}
    )
    return resp


@pytest.fixture
def live_server():
    """启动带线程池的临时 WSGI 服务，用于真实并发请求测试"""
    class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    class _QuietHandler(WSGIRequestHandler):
        def log_message(self, *args):
            pass

    from app import app
    server = make_server(
        '127.0.0.1', 0, app,
        server_class=ThreadingWSGIServer,
        handler_class=_QuietHandler
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{port}'
    server.shutdown()


def _http_get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_remaining_one_last_download_succeeds(client, auth_token):
    """剩余一次时详情页显示可下载，真正取件成功，之后才失效（边界不错乱）"""
    file_id = _upload_file(client, 'last_one.txt', b'last')
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=24, max_downloads=1
    ).get_json()['share_id']

    info = client.get(f'/api/share/{share_id}').get_json()
    assert info['is_valid'] is True
    assert info['remaining_downloads'] == 1
    assert info['download_count'] == 0

    resp = client.get(f'/api/share/{share_id}/download')
    assert resp.status_code == 200
    assert resp.data == b'last'

    info_after = client.get(f'/api/share/{share_id}').get_json()
    assert info_after['is_valid'] is False
    assert info_after['error_msg'] == '分享链接下载次数已用完'
    assert info_after['remaining_downloads'] == 0
    assert info_after['download_count'] == 1

    again = client.get(f'/api/share/{share_id}/download')
    assert again.status_code == 404
    assert '已用完' in again.get_json()['error']

    # 失败的取件不能重复计数
    assert client.get(f'/api/share/{share_id}').get_json()['download_count'] == 1


def test_zero_max_downloads_rejected(client, auth_token):
    """零次上限的链接不允许创建"""
    file_id = _upload_file(client)
    resp = _create_share(client, auth_token, file_id, expire_hours=24, max_downloads=0)
    assert resp.status_code == 400


def test_expiry_boundary_is_inclusive(client, auth_token, db_conn):
    """时钟恰好走到过期时刻：详情、列表、取件三处一致判定为已过期"""
    file_id = _upload_file(client)
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=1, max_downloads=5
    ).get_json()['share_id']

    boundary = time.time()
    cursor = db_conn.cursor()
    cursor.execute(
        'UPDATE share_links SET expires_at = ? WHERE id = ?',
        (boundary, share_id)
    )
    db_conn.commit()

    # 模拟与过期时刻完全相同的“现在”（<= 即过期）
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr('routes.file_routes.time.time', lambda: boundary)
        detail = client.get(f'/api/share/{share_id}').get_json()
        assert detail['is_valid'] is False
        assert detail['error_msg'] == '分享链接已过期'

        download_resp = client.get(f'/api/share/{share_id}/download')
        assert download_resp.status_code == 404
        assert '已过期' in download_resp.get_json()['error']

        lst = client.get(
            '/api/shares',
            headers={'Authorization': f'Bearer {auth_token}'}
        ).get_json()
        mine = next(s for s in lst if s['share_id'] == share_id)
        assert mine['is_valid'] is False
        assert mine['error_msg'] == '分享链接已过期'

    # 已过期的取件不计数
    assert client.get(f'/api/share/{share_id}').get_json()['download_count'] == 0


def test_concurrent_downloads_never_exceed_limit(client, auth_token, db_conn, live_server):
    """快速并发取件不能超过上限：恰好成功 max_downloads 次，计数精确"""
    file_id = _upload_file(client, 'concurrent.txt', b'concurrent data')
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=24, max_downloads=5
    ).get_json()['share_id']

    url = f'{live_server}/api/share/{share_id}/download'
    barrier = threading.Barrier(20)
    results = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait()
        status, _body = _http_get(url)
        with results_lock:
            results.append(status)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 5
    assert results.count(404) == 15

    row = db_conn.execute(
        'SELECT download_count, max_downloads FROM share_links WHERE id = ?',
        (share_id,)
    ).fetchone()
    assert row['download_count'] == 5
    assert row['download_count'] <= row['max_downloads']

    info = client.get(f'/api/share/{share_id}').get_json()
    assert info['is_valid'] is False
    assert info['remaining_downloads'] == 0


def test_unlimited_link_remains_valid_long_term(client, auth_token):
    """原有未设上限/永久有效的链接：大量下载与跨天后仍长期可用"""
    file_id = _upload_file(client, 'forever.txt', b'forever')
    result = _create_share(
        client, auth_token, file_id, expire_hours=-1, max_downloads=-1
    ).get_json()
    share_id = result['share_id']
    assert result['expires_at'] is None
    assert result['max_downloads'] is None

    for _ in range(15):
        resp = client.get(f'/api/share/{share_id}/download')
        assert resp.status_code == 200

    # 模拟时钟跨天（48 小时后）：永久链接不受影响
    future = time.time() + 48 * 3600
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr('routes.file_routes.time.time', lambda: future)
        detail = client.get(f'/api/share/{share_id}').get_json()
        assert detail['is_valid'] is True
        assert detail['remaining_downloads'] is None
        assert client.get(f'/api/share/{share_id}/download').status_code == 200


def test_list_and_public_page_same_status(client, auth_token, db_conn):
    """创建者列表和公开详情对同一条链接给出完全相同的状态"""
    file_id = _upload_file(client, 'same.txt', b'same')
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=1, max_downloads=2
    ).get_json()['share_id']

    # 用掉一次：剩一次，两边都应有效
    assert client.get(f'/api/share/{share_id}/download').status_code == 200

    detail = client.get(f'/api/share/{share_id}').get_json()
    listed = client.get(
        '/api/shares', headers={'Authorization': f'Bearer {auth_token}'}
    ).get_json()
    mine = next(s for s in listed if s['share_id'] == share_id)

    for key in ('is_valid', 'error_msg', 'download_count', 'max_downloads',
                'remaining_downloads', 'expires_at'):
        assert detail[key] == mine[key]
    assert detail['is_valid'] is True
    assert detail['remaining_downloads'] == 1

    # 再用掉两次（最后一次成功 + 一次失败）后两边都应失效且计数一致
    assert client.get(f'/api/share/{share_id}/download').status_code == 200
    assert client.get(f'/api/share/{share_id}/download').status_code == 404

    detail = client.get(f'/api/share/{share_id}').get_json()
    listed = client.get(
        '/api/shares', headers={'Authorization': f'Bearer {auth_token}'}
    ).get_json()
    mine = next(s for s in listed if s['share_id'] == share_id)

    assert detail['is_valid'] is False
    assert mine['is_valid'] is False
    assert detail['error_msg'] == mine['error_msg'] == '分享链接下载次数已用完'
    assert detail['download_count'] == mine['download_count'] == 2


def test_duplicate_submit_does_not_double_count(client, auth_token, live_server):
    """同一取件动作快速重复提交（只剩一次时）只能成功一次，计数为 1"""
    file_id = _upload_file(client, 'double.txt', b'double')
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=24, max_downloads=1
    ).get_json()['share_id']

    url = f'{live_server}/api/share/{share_id}/download'
    barrier = threading.Barrier(2)
    statuses = []
    lock = threading.Lock()

    def hit():
        barrier.wait()
        status, _body = _http_get(url)
        with lock:
            statuses.append(status)

    threads = [threading.Thread(target=hit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(statuses) == [200, 404]
    assert client.get(f'/api/share/{share_id}').get_json()['download_count'] == 1


def test_failed_download_does_not_consume_quota(client, auth_token, db_conn):
    """服务端原因导致无法取件时不应消耗下载名额"""
    file_id = _upload_file(client, 'missing.txt', b'missing')
    share_id = _create_share(
        client, auth_token, file_id, expire_hours=24, max_downloads=1
    ).get_json()['share_id']

    import os
    from config import UPLOAD_FOLDER
    row = db_conn.execute('SELECT path FROM files WHERE id = ?', (file_id,)).fetchone()
    os.remove(os.path.join(UPLOAD_FOLDER, os.path.basename(row['path'])))

    resp = client.get(f'/api/share/{share_id}/download')
    assert resp.status_code == 404

    info = client.get(f'/api/share/{share_id}').get_json()
    assert info['download_count'] == 0
