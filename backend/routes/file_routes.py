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


def get_share_link_info(share_id):
    """获取分享链接信息，包含文件信息和有效性检查"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.id, s.file_id, s.created_by, s.expires_at, s.max_downloads, s.download_count, s.created_at,
               f.name as filename, f.size as filesize
        FROM share_links s
        JOIN files f ON s.file_id = f.id
        WHERE s.id = ?
    ''', (share_id,))
    share = cursor.fetchone()
    conn.close()
    return share


def is_share_valid(share):
    """检查分享链接是否有效。

    这是唯一的有效性判定入口，公开详情、创建者列表和取件
    必须共用它，保证同一链接在任何地方给出相同状态。
    判断一律使用服务器端 epoch 秒时间戳，与客户端时区、
    跨天无关。
    """
    if not share:
        return False, '分享链接不存在'

    if share['expires_at'] is not None and share['expires_at'] < time.time():
        return False, '分享链接已过期'

    download_count = share['download_count'] or 0
    if share['max_downloads'] is not None and download_count >= share['max_downloads']:
        return False, '分享链接下载次数已用完'

    return True, None


def try_consume_share_download(conn, share_id):
    """原子地占用一次下载配额。

    用单条条件 UPDATE 同时完成“是否还能下载”的判断和
    计数 +1，消除“先检查后计数”的竞态（TOCTOU），
    并发取件、重复提交都不会让 download_count 超过
    max_downloads。未设上限（max_downloads IS NULL）或
    永久有效（expires_at IS NULL）的链接不受限制。

    返回 (ok, reason, new_count)。
    """
    now = time.time()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE share_links
        SET download_count = COALESCE(download_count, 0) + 1
        WHERE id = ?
          AND (expires_at IS NULL OR expires_at >= ?)
          AND (max_downloads IS NULL OR COALESCE(download_count, 0) < max_downloads)
    ''', (share_id, now))

    if cursor.rowcount == 1:
        conn.commit()
        cursor.execute(
            'SELECT download_count FROM share_links WHERE id = ?',
            (share_id,)
        )
        return True, None, cursor.fetchone()['download_count']

    # 未占用成功，查明具体原因，保持与 is_share_valid 相同的错误语义
    cursor.execute(
        'SELECT expires_at, max_downloads, download_count FROM share_links WHERE id = ?',
        (share_id,)
    )
    row = cursor.fetchone()
    if row is None:
        return False, '分享链接不存在', 0
    if row['expires_at'] is not None and now >= row['expires_at']:
        return False, '分享链接已过期', row['download_count']
    return False, '分享链接下载次数已用完', row['download_count']


def serialize_share(share):
    """把分享记录序列化为对外统一结构。

    创建者列表（/api/shares）和公开详情（/api/share/<id>）
    都必须经过这里，确保同一链接的状态、次数、剩余量等
    字段完全一致，不存在两处各拼一套的情况。
    """
    valid, error_msg = is_share_valid(share)
    download_count = share['download_count'] or 0
    remaining = None
    if share['max_downloads'] is not None:
        remaining = max(0, share['max_downloads'] - download_count)

    return {
        'share_id': share['id'],
        'file_id': share['file_id'],
        'filename': share['filename'],
        'filesize': share['filesize'],
        'created_by': share['created_by'],
        'expires_at': share['expires_at'],
        'max_downloads': share['max_downloads'],
        'download_count': download_count,
        'remaining_downloads': remaining,
        'created_at': share['created_at'],
        'is_valid': valid,
        'error_msg': error_msg
    }


def normalize_expire_hours(expire_hours):
    """规范化有效期参数：None 用默认值，负数表示永久，0/非法拒绝。

    返回 (expire_hours, error)；expire_hours 为 None 表示永久有效。
    """
    if expire_hours is None:
        expire_hours = SHARE_LINK_EXPIRE_HOURS
    try:
        expire_hours = int(expire_hours)
    except (TypeError, ValueError):
        return None, '有效期格式不正确'
    if expire_hours < 0:
        return None, None  # -1 表示永久有效
    if expire_hours == 0:
        return None, '有效期必须大于 0（如需长期有效请选择永久有效）'
    return expire_hours, None


def normalize_max_downloads(max_downloads):
    """规范化下载次数参数：None 用默认值，负数表示无限制，0 拒绝。

    返回 (max_downloads, error)；max_downloads 为 None 表示无限制。
    """
    if max_downloads is None:
        max_downloads = SHARE_LINK_MAX_DOWNLOADS
    try:
        max_downloads = int(max_downloads)
    except (TypeError, ValueError):
        return None, '下载次数格式不正确'
    if max_downloads < 0:
        return None, None  # -1 表示无限制
    if max_downloads == 0:
        return None, '最大下载次数不能为 0（如需不限次数请选择无限制）'
    return max_downloads, None


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

    file_id = data.get('file_id', '').strip()
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

    expire_hours, expire_error = normalize_expire_hours(expire_hours)
    if expire_error:
        conn.close()
        return jsonify({'error': expire_error}), 400

    if expire_hours is not None:
        expires_at = time.time() + expire_hours * 3600
    else:
        expires_at = None

    max_downloads, downloads_error = normalize_max_downloads(max_downloads)
    if downloads_error:
        conn.close()
        return jsonify({'error': downloads_error}), 400

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
    """获取分享链接信息（公开访问）"""
    share = get_share_link_info(share_id)

    if not share:
        return jsonify({'error': '分享链接不存在'}), 404

    # 与创建者列表共用同一序列化/判定逻辑，状态必然一致
    return jsonify(serialize_share(share))


@files_bp.route('/api/share/<share_id>/download', methods=['GET'])
def download_by_share(share_id):
    """通过分享链接下载文件（公开访问）。

    计数更新与有效性判断在同一个事务中原子完成，
    并发取件或重复提交都不会超过 max_downloads。
    """
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.id, s.file_id, s.created_by, s.expires_at,
               s.max_downloads, s.download_count, s.created_at,
               f.name as filename, f.size as filesize
        FROM share_links s
        JOIN files f ON s.file_id = f.id
        WHERE s.id = ?
    ''', (share_id,))
    share = cursor.fetchone()

    if not share:
        conn.close()
        return jsonify({'error': '分享链接不存在'}), 404

    # 快速预检，给出与详情页一致的错误信息（过期优先于次数用完）
    valid, error_msg = is_share_valid(share)
    if not valid:
        conn.close()
        return jsonify({'error': error_msg}), 404

    cursor.execute('SELECT name, path FROM files WHERE id = ?', (share['file_id'],))
    file_info = cursor.fetchone()

    if not file_info:
        conn.close()
        return jsonify({'error': '文件不存在'}), 404

    if not os.path.abspath(file_info['path']).startswith(os.path.abspath(UPLOAD_FOLDER)):
        conn.close()
        return jsonify({'error': '非法文件路径'}), 403

    if not os.path.exists(file_info['path']):
        conn.close()
        return jsonify({'error': '文件不存在'}), 404

    # 原子占用配额：并发请求中只有真正抢到名额的才会走到发送文件
    ok, reason, new_count = try_consume_share_download(conn, share_id)
    conn.close()

    if not ok:
        return jsonify({'error': reason}), 404

    logger.info(f"分享下载: 文件 {file_info['name']}, 分享ID {share_id}, 下载次数 {new_count}")
    return send_file(file_info['path'], as_attachment=True, download_name=file_info['name'])


@files_bp.route('/api/shares', methods=['GET'])
@login_required
def list_shares():
    """获取当前用户的所有分享链接"""
    token = get_token_from_request()
    username = get_username_from_token(token)

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT s.id, s.file_id, s.created_by, s.expires_at, s.max_downloads, s.download_count, s.created_at,
               f.name as filename, f.size as filesize
        FROM share_links s
        JOIN files f ON s.file_id = f.id
        WHERE s.created_by = ?
        ORDER BY s.created_at DESC
    ''', (username,))
    shares = cursor.fetchall()
    conn.close()

    # 与公开详情共用同一序列化/判定逻辑，保证状态一致
    result = [serialize_share(share) for share in shares]

    return jsonify(result)


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
