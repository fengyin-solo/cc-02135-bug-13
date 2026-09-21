"""文件路由"""
import os
import re
import uuid
import time
import logging
from flask import request, jsonify, send_file
from werkzeug.utils import secure_filename
from routes import files_bp
from database import get_db
from auth import verify_token, get_username_from_token, login_required
from config import UPLOAD_FOLDER, MAX_FILE_SIZE, BLOCKED_EXTENSIONS, SHARE_LINK_EXPIRE_HOURS, SHARE_LINK_MAX_DOWNLOADS

logger = logging.getLogger(__name__)


def allowed_file(filename):
    """检查文件扩展名是否被禁止"""
    if '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext not in BLOCKED_EXTENSIONS


@files_bp.route('/api/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': '没有文件'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '未选择文件'}), 400

    if not allowed_file(file.filename):
        return jsonify({'error': '不支持的文件类型'}), 400

    file.seek(0, 2)
    file_size = file.tell()
    file.seek(0)

    if file_size > MAX_FILE_SIZE:
        return jsonify({'error': f'文件大小超过限制（最大{MAX_FILE_SIZE // 1024 // 1024}MB）'}), 400

    file_id = str(uuid.uuid4())
    # 保留原始文件名用于显示（去掉路径分隔符防止注入）
    original_name = re.sub(r'[/\\]', '_', file.filename).strip()
    if not original_name:
        original_name = file_id

    # 磁盘上用 UUID + 扩展名存储，避免文件名编码问题
    ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
    safe_filename = f"{file_id}.{ext}" if ext else file_id
    filepath = os.path.join(UPLOAD_FOLDER, safe_filename)
    file.save(filepath)

    file_size = os.path.getsize(filepath)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO files (id, name, path, size) VALUES (?, ?, ?, ?)',
        (file_id, original_name, filepath, file_size)
    )
    conn.commit()
    conn.close()

    logger.info(f"文件上传成功: {original_name} (ID: {file_id}, 大小: {file_size} bytes)")
    return jsonify({'success': True, 'file_id': file_id, 'filename': original_name})


@files_bp.route('/api/files', methods=['GET'])
def list_files():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name, path, size FROM files')
    files = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return jsonify(files)


@files_bp.route('/api/download/<file_id>', methods=['GET'])
def download_file(file_id):
    # 优先从 Authorization 头获取 token，兼容查询参数（已废弃）
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
    else:
        token = request.args.get('token')  # 向后兼容，建议前端迁移到 Authorization 头

    if not token or not verify_token(token):
        return jsonify({'error': '未授权或token已过期'}), 401

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT name, path FROM files WHERE id = ?', (file_id,))
    file_info = cursor.fetchone()
    conn.close()

    if not file_info:
        return jsonify({'error': '文件不存在'}), 404

    if not os.path.abspath(file_info['path']).startswith(os.path.abspath(UPLOAD_FOLDER)):
        return jsonify({'error': '非法文件路径'}), 403

    if not os.path.exists(file_info['path']):
        return jsonify({'error': '文件不存在'}), 404

    logger.info(f"文件下载: {file_info['name']} (ID: {file_id})")
    return send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])


def generate_short_id():
    """生成短的分享链接ID"""
    return uuid.uuid4().hex[:12]


# 分享链接查询的统一字段（创建者列表与公开详情必须使用同一份数据）
SHARE_SELECT_SQL = '''
    SELECT s.id, s.file_id, s.created_by, s.expires_at, s.max_downloads,
           COALESCE(s.download_count, 0) AS download_count, s.created_at,
           f.name as filename, f.size as filesize
    FROM share_links s
    JOIN files f ON s.file_id = f.id
'''


def get_share_link_info(share_id):
    """获取分享链接信息，包含文件信息"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(SHARE_SELECT_SQL + ' WHERE s.id = ?', (share_id,))
    share = cursor.fetchone()
    conn.close()
    return share


def is_share_valid(share, now=None):
    """检查分享链接是否有效。

    判定规则（创建者列表、公开详情、取件共用，保证状态一致）：
    - expires_at 为空表示永久有效；过期时刻（<= 当前时间）即失效，边界与前端一致
    - max_downloads 为空表示不限次数；download_count 达到上限（含上限为 0 的边界）即失效
    """
    if share is None:
        return False, '分享链接不存在'

    if now is None:
        now = time.time()

    if share['expires_at'] is not None and share['expires_at'] <= now:
        return False, '分享链接已过期'

    if share['max_downloads'] is not None and share['download_count'] >= share['max_downloads']:
        return False, '分享链接下载次数已用完'

    return True, None


def serialize_share(share, now=None):
    """把分享链接记录序列化为接口响应。

    列表接口和公开详情接口共用此函数，确保同一条链接在任何页面状态完全一致。
    """
    valid, error_msg = is_share_valid(share, now)
    remaining = None
    if share['max_downloads'] is not None:
        remaining = max(0, share['max_downloads'] - share['download_count'])

    return {
        'share_id': share['id'],
        'file_id': share['file_id'],
        'filename': share['filename'],
        'filesize': share['filesize'],
        'created_by': share['created_by'],
        'expires_at': share['expires_at'],
        'max_downloads': share['max_downloads'],
        'download_count': share['download_count'],
        'remaining_downloads': remaining,
        'created_at': share['created_at'],
        'is_valid': valid,
        'error_msg': error_msg
    }


def claim_share_download(share_id, now=None):
    """原子地占用一次下载名额。

    用单条带条件的 UPDATE 完成“有效性检查 + 计数加一”，由数据库串行化写入，
    避免先查后改（TOCTOU）导致的并发取件超额和重复提交重复计数。

    返回 (claimed, share, error_msg)：
    - claimed=True：名额占用成功，share 为更新后的记录
    - claimed=False：链接不存在/已过期/次数已用完，error_msg 为原因
    """
    if now is None:
        now = time.time()

    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE share_links
            SET download_count = COALESCE(download_count, 0) + 1
            WHERE id = ?
              AND (expires_at IS NULL OR expires_at > ?)
              AND (max_downloads IS NULL OR COALESCE(download_count, 0) < max_downloads)
        ''', (share_id, now))
        claimed = cursor.rowcount == 1
        conn.commit()
    finally:
        conn.close()

    share = get_share_link_info(share_id)
    if claimed:
        return True, share, None

    if share is None:
        return False, None, '分享链接不存在'

    _valid, error_msg = is_share_valid(share, now)
    return False, share, error_msg or '分享链接已失效'


def get_token_from_request():
    """从请求中获取 token"""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    return request.args.get('token')


@files_bp.route('/api/share', methods=['POST'])
@login_required
def create_share():
    """创建分享链接"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '无效的请求数据'}), 400

    file_id = data.get('file_id', '')
    if isinstance(file_id, str):
        file_id = file_id.strip()
    expire_hours = data.get('expire_hours')
    max_downloads = data.get('max_downloads')

    if not file_id:
        return jsonify({'error': '文件ID不能为空'}), 400

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id, name FROM files WHERE id = ?', (file_id,))
    file_info = cursor.fetchone()

    if not file_info:
        conn.close()
        return jsonify({'error': '文件不存在'}), 404

    # 有效期：缺省用默认值；负数表示永久有效；不接受非数值
    if expire_hours is None:
        expire_hours = SHARE_LINK_EXPIRE_HOURS
    if not isinstance(expire_hours, (int, float)) or isinstance(expire_hours, bool):
        conn.close()
        return jsonify({'error': '有效期必须为数字'}), 400

    if expire_hours < 0:
        expires_at = None
    else:
        expires_at = time.time() + expire_hours * 3600

    # 下载次数：缺省用默认值；负数表示不限次数；必须为正整数，0 次链接没有意义
    if max_downloads is None:
        max_downloads = SHARE_LINK_MAX_DOWNLOADS
    if isinstance(max_downloads, bool) or not isinstance(max_downloads, int):
        conn.close()
        return jsonify({'error': '下载次数必须为整数'}), 400

    if max_downloads < 0:
        max_downloads = None
    elif max_downloads == 0:
        conn.close()
        return jsonify({'error': '下载次数必须大于 0'}), 400

    token = get_token_from_request()
    username = get_username_from_token(token)

    share_id = generate_short_id()

    cursor.execute('''
        INSERT INTO share_links (id, file_id, created_by, expires_at, max_downloads)
        VALUES (?, ?, ?, ?, ?)
    ''', (share_id, file_id, username, expires_at, max_downloads))

    conn.commit()
    conn.close()

    logger.info(f"分享链接创建成功: 文件 {file_info['name']}, 分享ID {share_id}, 创建者 {username}")

    return jsonify({
        'success': True,
        'share_id': share_id,
        'expires_at': expires_at,
        'max_downloads': max_downloads,
        'filename': file_info['name']
    })


@files_bp.route('/api/share/<share_id>', methods=['GET'])
def get_share(share_id):
    """获取分享链接信息（公开访问）。

    链接已过期/次数用完时仍返回 200 并携带 is_valid=False，由前端展示失效页；
    状态内容与创建者列表接口完全一致。
    """
    share = get_share_link_info(share_id)

    if not share:
        return jsonify({'error': '分享链接不存在'}), 404

    response = jsonify(serialize_share(share))
    # 状态随每次请求实时计算，禁止浏览器/代理缓存，避免过期页刷新显示旧状态
    response.headers['Cache-Control'] = 'no-store'
    return response


@files_bp.route('/api/share/<share_id>/download', methods=['GET'])
def download_by_share(share_id):
    """通过分享链接下载文件（公开访问）。

    先校验链接与文件，再原子占用下载名额，保证并发取件和重复提交都不会超额或误计数，
    文件缺失等服务端问题也不会白白消耗下载次数。
    """
    share = get_share_link_info(share_id)
    if not share:
        return jsonify({'error': '分享链接不存在'}), 404

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT name, path FROM files WHERE id = ?', (share['file_id'],))
    file_info = cursor.fetchone()
    conn.close()

    if not file_info or not os.path.exists(file_info['path']):
        return jsonify({'error': '文件不存在'}), 404

    if not os.path.abspath(file_info['path']).startswith(os.path.abspath(UPLOAD_FOLDER)):
        return jsonify({'error': '非法文件路径'}), 403

    now = time.time()
    claimed, updated_share, error_msg = claim_share_download(share_id, now)
    if not claimed:
        response = jsonify({'error': error_msg})
        response.headers['Cache-Control'] = 'no-store'
        return response, 404

    logger.info(
        f"分享下载: 文件 {file_info['name']}, 分享ID {share_id}, "
        f"已用次数 {updated_share['download_count']}/{updated_share['max_downloads'] or '不限'}"
    )
    return send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])


@files_bp.route('/api/shares', methods=['GET'])
@login_required
def list_shares():
    """获取当前用户的所有分享链接"""
    token = get_token_from_request()
    username = get_username_from_token(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        SHARE_SELECT_SQL + '''
            WHERE s.created_by = ?
            ORDER BY s.created_at DESC
        ''',
        (username,)
    )
    shares = cursor.fetchall()
    conn.close()

    # 与公开详情接口共用同一序列化逻辑，保证同一条链接两处状态一致
    now = time.time()
    result = [serialize_share(share, now) for share in shares]
    response = jsonify(result)
    response.headers['Cache-Control'] = 'no-store'
    return response


@files_bp.route('/api/share/<share_id>', methods=['DELETE'])
@login_required
def delete_share(share_id):
    """删除分享链接"""
    token = get_token_from_request()
    username = get_username_from_token(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT created_by, file_id FROM share_links WHERE id = ?', (share_id,))
    share = cursor.fetchone()

    if not share:
        conn.close()
        return jsonify({'error': '分享链接不存在'}), 404

    if share['created_by'] != username:
        conn.close()
        return jsonify({'error': '无权限删除此分享链接'}), 403

    cursor.execute('DELETE FROM share_links WHERE id = ?', (share_id,))
    conn.commit()
    conn.close()

    logger.info(f"分享链接删除: 分享ID {share_id}, 文件ID {share['file_id']}, 操作者 {username}")
    return jsonify({'success': True, 'message': '分享链接已删除'})
