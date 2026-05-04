import base64
import hashlib
import json
import logging
import os
import os.path
import time
import uuid
from datetime import date
from urllib import parse

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

token_save_name = 'TOKEN.txt'
token_env = os.environ.get('TOKEN')
exit_when_fail_env = os.environ.get('EXIT_WHEN_FAIL')
current_type = os.environ.get('SKYLAND_TYPE')
role_ids_env = os.environ.get('TGD_ROLE_IDS')
game_id_env = os.environ.get('TGD_GAME_ID')
sign_game_ids_env = os.environ.get('TGD_SIGN_GAME_IDS')
no_pause_env = os.environ.get('NO_PAUSE')

DEFAULT_GAME_ID = '1289'
COMMUNITY_ID = '1'
APP_ID = '10550'
USER_CENTER_APP_ID = '10551'
SECRET = '89155cc4e8634ec5b1b6364013b23e3e'
DEVICETYPE = 'LGE-AN10'
TYPE = '16'
DEVICENAME = 'LGE-AN10'
VERSIONCODE = '1'
AREACODEID = '1'
DEVICESYS = '12'
DEVICEMODEL = 'LGE-AN10'
SDKVERSION = '4.129.0'
BID = 'com.pwrd.htassistant'
CHANNELID = '1'
# usercenter/login + refreshToken 对 appversion 校验严格，当前可用值是 1.1.0
APPVERSION = '1.1.0'
OKHTTP_UA = 'okhttp/4.12.0'
WEBVIEW_UA = (
    'Mozilla/5.0 (Linux; Android 15; TB321FU Build/AQ3A.240912.001; wv) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 '
    'Chrome/148.0.7778.28 Safari/537.36'
)

REQUEST_HEADERS_BASE = {
    'platform': 'android',
    'Content-Type': 'application/x-www-form-urlencoded',
}

SEND_CAPTCHA_URL = 'https://user.laohu.com/m/newApi/sendPhoneCaptchaWithOutLogin'
CHECK_CAPTCHA_URL = 'https://user.laohu.com/m/newApi/checkPhoneCaptchaWithOutLogin'
LOGIN_URL = 'https://user.laohu.com/openApi/sms/new/login'
PASSWORD_LOGIN_URL = 'https://user.laohu.com/m/newApi/login'
USER_CENTER_LOGIN_URL = 'https://bbs-api.tajiduo.com/usercenter/api/login'
REFRESH_TOKEN_URL = 'https://bbs-api.tajiduo.com/usercenter/api/refreshToken'
GET_GAME_ROLES_URL = 'https://bbs-api.tajiduo.com/usercenter/api/v2/getGameRoles'
APP_SIGNIN_URL = 'https://bbs-api.tajiduo.com/apihub/api/signin'
GAME_SIGNIN_URL = 'https://bbs-api.tajiduo.com/apihub/awapi/sign'
GAME_SIGNIN_STATE_URL = 'https://bbs-api.tajiduo.com/apihub/awapi/signin/state'
GAME_SIGN_REWARDS_URL = 'https://bbs-api.tajiduo.com/apihub/awapi/sign/rewards'


def config_logger():
    current_date = date.today().strftime('%Y-%m-%d')
    if not os.path.exists('logs'):
        os.mkdir('logs')
    logger = logging.getLogger()

    file_handler = logging.FileHandler(f'./logs/{current_date}.log', encoding='utf-8')
    logger.addHandler(file_handler)
    logging.getLogger().setLevel(logging.DEBUG)
    file_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(message)s')
    file_handler.setFormatter(formatter)

    def scrub(value):
        filter_key = {'code', 'cred', 'token', 'authorization', 'refreshtoken', 'accesstoken'}
        if isinstance(value, dict):
            masked = {}
            for k, v in value.items():
                if str(k).lower() in filter_key:
                    masked[k] = '*****'
                else:
                    masked[k] = scrub(v)
            return masked
        if isinstance(value, list):
            return [scrub(i) for i in value]
        return value

    def compact_data(data):
        if isinstance(data, list):
            return {'count': len(data)}
        if not isinstance(data, dict):
            return data

        compact = {}
        for key in ('uid', 'userId', 'bindRole', 'todaySign', 'day', 'days', 'month', 'reSignCnt', 'firstLogin'):
            if key in data:
                compact[key] = data[key]

        roles = data.get('roles')
        if isinstance(roles, list):
            compact['rolesCount'] = len(roles)
            if roles and isinstance(roles[0], dict):
                role = {}
                for key in ('gameId', 'roleId', 'roleName', 'lev', 'serverName'):
                    if key in roles[0]:
                        role[key] = roles[0][key]
                if role:
                    compact['firstRole'] = role

        if not compact:
            compact['keys'] = list(data.keys())[:5]
        return compact

    def compact_payload(text):
        try:
            payload = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            plain = str(text).replace('\n', ' ').strip()
            return plain[:180] + ('...' if len(plain) > 180 else '')

        payload = scrub(payload)
        if isinstance(payload, dict):
            compact = {}
            for key in ('code', 'ok', 'msg', 'message'):
                if key in payload:
                    compact[key] = payload[key]
            if 'data' in payload:
                compact['data'] = compact_data(payload.get('data'))
            elif 'result' in payload:
                compact['result'] = compact_data(payload.get('result'))
            payload = compact or compact_data(payload)
        elif isinstance(payload, list):
            payload = {'count': len(payload)}

        return json.dumps(scrub(payload), ensure_ascii=False, separators=(',', ':'))

    def compact_url(url):
        try:
            parsed = parse.urlparse(str(url))
            if parsed.netloc:
                return f'{parsed.netloc}{parsed.path}'
            if parsed.path:
                return parsed.path
        except Exception:
            pass
        return str(url)

    _get = requests.get
    _post = requests.post

    def get(*args, **kwargs):
        response = _get(*args, **kwargs)
        logger.info(f'GET {compact_url(args[0])} {response.status_code} | {compact_payload(response.text)}')
        return response

    def post(*args, **kwargs):
        response = _post(*args, **kwargs)
        logger.info(f'POST {compact_url(args[0])} {response.status_code} | {compact_payload(response.text)}')
        return response

    # 替换 requests 中的方法
    requests.get = get
    requests.post = post


def _dedup_list(items):
    result = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def generate_signature(params):
    sorted_keys = sorted(params.keys())
    values = ''.join(str(params[key]) for key in sorted_keys)
    return hashlib.md5((values + SECRET).encode('utf-8')).hexdigest()


def _aes_base64_encode(text):
    key = SECRET[-16:].encode('utf-8')
    padder = padding.PKCS7(128).padder()
    padded = padder.update(text.encode('utf-8')) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode('utf-8')


def _random_device_id():
    return uuid.uuid4().hex


def _default_game_id():
    v = (game_id_env or DEFAULT_GAME_ID).strip()
    return v if v else DEFAULT_GAME_ID


def _candidate_sign_game_ids(primary_game_id):
    candidates = []
    env_candidates = _parse_role_ids(sign_game_ids_env)
    if env_candidates:
        candidates.extend(env_candidates)
    candidates.extend([
        str(primary_game_id or '').strip(),
        _default_game_id(),
        '1289',
        '1257',
    ])
    return _dedup_list(candidates)


def _parse_role_ids(role_text):
    if not role_text:
        return []
    if isinstance(role_text, list):
        return _dedup_list([str(i).strip() for i in role_text if str(i).strip()])
    text = str(role_text).replace('\n', ',')
    return _dedup_list([i.strip() for i in text.split(',') if i.strip()])


def _safe_json(response, endpoint):
    if not response.text.strip():
        raise Exception(f'{endpoint} 返回空响应，status={response.status_code}')
    try:
        return response.json()
    except json.JSONDecodeError as ex:
        raise Exception(f'{endpoint} 返回非JSON: {response.text[:200]}') from ex


def _request_form(url, data, headers):
    return requests.post(url, data=parse.urlencode(data), headers=headers)


def _request_json(url, data, headers):
    return requests.post(url, json=data, headers=headers)


def parse_account_line(line):
    line = line.strip()
    if not line:
        return None

    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return {
            'refreshToken': line,
            'uid': '',
            'deviceId': _random_device_id(),
            'gameId': _default_game_id(),
            'roleIds': [],
        }

    if not isinstance(raw, dict):
        raise ValueError('账号内容必须是JSON对象')

    refresh_token = str(raw.get('refreshToken') or raw.get('token') or '').strip()
    if not refresh_token:
        raise ValueError('账号缺少 refreshToken')

    uid = str(raw.get('uid') or '').strip()
    device_id = str(raw.get('deviceId') or raw.get('deviceid') or '').strip() or _random_device_id()
    game_id = str(raw.get('gameId') or raw.get('game_id') or _default_game_id()).strip() or _default_game_id()
    role_ids = _parse_role_ids(raw.get('roleIds') or raw.get('role_ids') or raw.get('roleId'))
    return {
        'refreshToken': refresh_token,
        'uid': uid,
        'deviceId': device_id,
        'gameId': game_id,
        'roleIds': role_ids,
    }


def _account_to_line(account):
    payload = {
        'refreshToken': account['refreshToken'],
        'uid': account.get('uid', ''),
        'deviceId': account.get('deviceId', _random_device_id()),
        'gameId': account.get('gameId', _default_game_id()),
        'roleIds': _dedup_list(account.get('roleIds', [])),
    }
    return json.dumps(payload, ensure_ascii=False)


def save(accounts):
    with open(token_save_name, 'w', encoding='utf-8') as f:
        f.write('\n'.join(_account_to_line(i) for i in accounts))
    print(f'账号信息已保存在 {token_save_name}，下次运行会直接签到。')


def read(path):
    if not os.path.exists(path):
        return []

    accounts = []
    with open(path, 'r', encoding='utf-8') as f:
        for row in f.readlines():
            row = row.strip()
            if not row:
                continue
            account = parse_account_line(row)
            if account:
                accounts.append(account)
    return accounts


def _mask_token(token):
    text = str(token or '').strip()
    if not text:
        return '无'
    if len(text) <= 10:
        return f'{text[:2]}***{text[-2:]}'
    return f'{text[:4]}***{text[-4:]}'


def _select_accounts(accounts):
    if len(accounts) <= 1:
        return accounts

    print('检测到多个账号，请选择要签到的账号：')
    for idx, account in enumerate(accounts, start=1):
        uid = str(account.get('uid') or '').strip() or '未设置'
        game_id = str(account.get('gameId') or _default_game_id()).strip() or _default_game_id()
        role_count = len(_dedup_list(account.get('roleIds', [])))
        token_tail = _mask_token(account.get('refreshToken'))
        print(f'{idx}. uid={uid} | gameId={game_id} | roleIds={role_count} | token={token_tail}')

    while True:
        raw = input('请输入序号（支持多个，用逗号分隔；回车或 all 为全部）：').strip()
        if not raw:
            return accounts
        if raw.lower() in {'all', 'a'}:
            return accounts

        picked = []
        invalid = []
        for part in raw.replace('，', ',').split(','):
            text = part.strip()
            if not text:
                continue
            if not text.isdigit():
                invalid.append(text)
                continue
            idx = int(text)
            if idx < 1 or idx > len(accounts):
                invalid.append(text)
                continue
            if idx not in picked:
                picked.append(idx)

        if picked and not invalid:
            return [accounts[i - 1] for i in picked]
        print(f'输入无效：{", ".join(invalid) if invalid else raw}，请重新输入。')


def _env_items():
    if not token_env:
        return []
    rows = [i.strip() for i in token_env.replace('\r\n', '\n').split('\n') if i.strip()]
    if len(rows) == 1 and ',' in rows[0] and not rows[0].lstrip().startswith('{'):
        return [i.strip() for i in rows[0].split(',') if i.strip()]
    return rows


def read_from_env():
    accounts = []
    for row in _env_items():
        account = parse_account_line(row)
        if account:
            accounts.append(account)
    print(f'从环境变量中读取到 {len(accounts)} 个账号...')
    return accounts


def _is_ok(resp):
    return resp.get('code') == 0


def _is_already_signed(message):
    for key in ['已签到', '签到过', '重复签到']:
        if key in message:
            return True
    return False


def send_captcha(phone, device_id):
    data = {
        'deviceType': DEVICETYPE,
        'type': TYPE,
        'deviceId': device_id,
        'deviceName': DEVICENAME,
        'versionCode': VERSIONCODE,
        't': str(int(time.time())),
        'areaCodeId': AREACODEID,
        'appId': APP_ID,
        'deviceSys': DEVICESYS,
        'cellphone': phone,
        'deviceModel': DEVICEMODEL,
        'sdkVersion': SDKVERSION,
        'bid': BID,
        'channelId': CHANNELID,
    }
    data['sign'] = generate_signature(data)
    resp = _safe_json(_request_form(SEND_CAPTCHA_URL, data, REQUEST_HEADERS_BASE), '发送验证码')
    if not _is_ok(resp):
        raise Exception(f'发送验证码失败：{resp.get("message") or resp.get("msg") or resp}')


def check_captcha(phone, code, device_id):
    data = {
        'deviceType': DEVICETYPE,
        'deviceId': device_id,
        'deviceName': DEVICENAME,
        'versionCode': VERSIONCODE,
        't': str(int(time.time())),
        'captcha': code,
        'appId': APP_ID,
        'deviceSys': DEVICESYS,
        'cellphone': phone,
        'deviceModel': DEVICEMODEL,
        'sdkVersion': SDKVERSION,
        'bid': BID,
        'channelId': CHANNELID,
    }
    data['sign'] = generate_signature(data)
    resp = _safe_json(_request_form(CHECK_CAPTCHA_URL, data, REQUEST_HEADERS_BASE), '验证验证码')
    if not _is_ok(resp):
        raise Exception(f'验证码错误：{resp.get("message") or resp.get("msg") or resp}')


def login(phone, code, device_id):
    data = {
        'deviceType': DEVICETYPE,
        'idfa': '',
        'sign': '',
        'adm': '',
        'type': TYPE,
        'deviceId': device_id,
        'version': VERSIONCODE,
        'deviceName': DEVICENAME,
        'mac': '',
        't': str(int(time.time() * 1000)),
        'areaCodeId': AREACODEID,
        'captcha': _aes_base64_encode(code),
        'appId': APP_ID,
        'deviceSys': DEVICESYS,
        'cellphone': _aes_base64_encode(phone),
        'deviceModel': DEVICEMODEL,
        'sdkVersion': SDKVERSION,
        'bid': BID,
        'channelId': CHANNELID,
    }
    data['sign'] = generate_signature(data)
    resp = _safe_json(_request_form(LOGIN_URL, data, REQUEST_HEADERS_BASE), '登录')
    if not _is_ok(resp):
        raise Exception(f'登录失败：{resp.get("message") or resp.get("msg") or resp}')
    result = resp.get('result') or {}
    token = result.get('token')
    user_id = result.get('userId')
    if not token or user_id is None:
        raise Exception(f'登录返回缺少 token/userId：{resp}')
    return token, str(user_id)


def _login_with_password_raw(phone, password, device_id, encrypt):
    username = _aes_base64_encode(phone) if encrypt else phone
    login_password = _aes_base64_encode(password) if encrypt else password
    data = {
        'deviceType': DEVICETYPE,
        'type': TYPE,
        'deviceId': device_id,
        'deviceName': DEVICENAME,
        'versionCode': VERSIONCODE,
        't': str(int(time.time())),
        'areaCodeId': AREACODEID,
        'appId': APP_ID,
        'deviceSys': DEVICESYS,
        'username': username,
        'password': login_password,
        'deviceModel': DEVICEMODEL,
        'sdkVersion': SDKVERSION,
        'bid': BID,
        'channelId': CHANNELID,
    }
    data['sign'] = generate_signature(data)
    return _safe_json(_request_form(PASSWORD_LOGIN_URL, data, REQUEST_HEADERS_BASE), '密码登录')


def login_with_password(phone, password, device_id):
    resp = _login_with_password_raw(phone, password, device_id, encrypt=False)
    if not _is_ok(resp):
        msg = str(resp.get('message') or resp.get('msg') or resp)
        if 'BAD_REQUEST' in msg:
            resp = _login_with_password_raw(phone, password, device_id, encrypt=True)
        if not _is_ok(resp):
            raise Exception(f'密码登录失败：{resp.get("message") or resp.get("msg") or resp}')
    result = resp.get('result') or {}
    token = result.get('token')
    user_id = result.get('userId')
    if not token or user_id is None:
        raise Exception(f'密码登录返回缺少 token/userId：{resp}')
    return token, str(user_id)


def user_center_login(token, user_id, device_id):
    headers = {
        **REQUEST_HEADERS_BASE,
        'deviceid': device_id,
        'authorization': '',
        'appversion': APPVERSION,
        'uid': '10000000',
        'User-Agent': OKHTTP_UA,
    }
    payload = {
        'token': token,
        'userIdentity': user_id,
        'appId': USER_CENTER_APP_ID,
    }
    resp = _safe_json(_request_form(USER_CENTER_LOGIN_URL, payload, headers), '用户中心登录')
    if not _is_ok(resp):
        raise Exception(f'用户中心登录失败：{resp.get("msg") or resp}')
    data = resp.get('data') or {}
    if not data.get('accessToken') or not data.get('refreshToken'):
        raise Exception(f'用户中心登录返回缺少accessToken/refreshToken：{resp}')
    return data


def refresh_access_token(account):
    headers = {
        **REQUEST_HEADERS_BASE,
        'deviceid': account['deviceId'],
        'authorization': account['refreshToken'],
        'appversion': APPVERSION,
        'uid': '10000000',
        'User-Agent': OKHTTP_UA,
    }
    response = requests.post(REFRESH_TOKEN_URL, headers=headers)
    if response.status_code == 402:
        raise Exception('refreshToken 已失效，请重新登录')
    resp = _safe_json(response, '刷新token')
    if not _is_ok(resp):
        raise Exception(f'刷新token失败：{resp.get("msg") or resp}')

    data = resp.get('data') or {}
    access_token = data.get('accessToken')
    refresh_token = data.get('refreshToken')
    if not access_token or not refresh_token:
        raise Exception(f'刷新token返回缺少accessToken/refreshToken：{resp}')
    account['refreshToken'] = refresh_token
    if data.get('uid'):
        account['uid'] = str(data['uid'])
    return access_token


def get_game_role_ids(access_token, uid, device_id, game_id):
    headers = {
        'platform': 'android',
        'authorization': access_token,
        'uid': uid,
        'deviceid': device_id,
        'appversion': APPVERSION,
        'User-Agent': OKHTTP_UA,
    }
    response = requests.get(GET_GAME_ROLES_URL, headers=headers, params={'gameId': game_id})
    resp = _safe_json(response, '获取角色列表')
    if not _is_ok(resp):
        raise Exception(f'获取角色列表失败：{resp.get("msg") or resp}')
    data = resp.get('data') or {}
    roles = data.get('roles', []) if isinstance(data, dict) else []
    role_ids = []
    for role in roles:
        role_id = str(role.get('roleId', '')).strip()
        if role_id:
            role_ids.append(role_id)
    return _dedup_list(role_ids)


def app_signin(access_token, uid, device_id):
    headers = {
        **REQUEST_HEADERS_BASE,
        'authorization': access_token,
        'uid': uid,
        'deviceid': device_id,
        'appversion': APPVERSION,
        'User-Agent': OKHTTP_UA,
    }
    response = _request_form(APP_SIGNIN_URL, {'communityId': COMMUNITY_ID}, headers)
    resp = _safe_json(response, '社区签到')
    if _is_ok(resp):
        data = resp.get('data') or {}
        exp = data.get('exp', 0)
        coin = data.get('goldCoin', 0)
        return True, f'社区签到成功，获得{exp}经验，{coin}金币'
    msg = str(resp.get('msg') or resp.get('message') or resp)
    if _is_already_signed(msg):
        return True, '社区今日已签到'
    return False, msg


def get_game_sign_state(access_token, game_id):
    response = requests.get(
        GAME_SIGNIN_STATE_URL,
        headers={'Authorization': access_token},
        params={'gameId': game_id},
    )
    resp = _safe_json(response, f'查询游戏签到状态(gameId={game_id})')
    if not _is_ok(resp):
        raise Exception(f'查询游戏签到状态失败(gameId={game_id})：{resp.get("msg") or resp}')
    data = resp.get('data') or {}
    if not isinstance(data, dict):
        raise Exception(f'查询游戏签到状态返回结构异常(gameId={game_id})：{resp}')
    return data


def get_game_sign_rewards(access_token, role_id, game_id):
    params = {'gameId': game_id}
    if role_id:
        params['roleId'] = role_id
    response = requests.get(
        GAME_SIGN_REWARDS_URL,
        headers={'Authorization': access_token},
        params=params,
    )
    resp = _safe_json(response, f'查询游戏签到奖励(gameId={game_id})')
    if not _is_ok(resp):
        raise Exception(f'查询游戏签到奖励失败(gameId={game_id})：{resp.get("msg") or resp}')

    data = resp.get('data')
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ['items', 'rewards', 'list']:
            items = data.get(key)
            if isinstance(items, list):
                return items
    raise Exception(f'查询游戏签到奖励返回结构异常(gameId={game_id})：{resp}')


def _format_reward_item_text(item):
    if not isinstance(item, dict):
        return ''
    name = str(item.get('name') or item.get('itemName') or item.get('title') or '').strip()
    if not name:
        return ''

    num = item.get('num')
    if num is None:
        num = item.get('count')
    if num is None:
        num = item.get('quantity')
    if num is None or str(num).strip() == '':
        return name
    return f'{name}x{num}'


def _today_reward_text(access_token, role_id, game_id, state_data=None):
    state = state_data if isinstance(state_data, dict) else get_game_sign_state(access_token, game_id)
    try:
        days = int(state.get('days') or 0)
    except (TypeError, ValueError):
        return ''
    if days <= 0:
        return ''

    rewards = get_game_sign_rewards(access_token, role_id, game_id)
    if len(rewards) < days:
        return ''
    return _format_reward_item_text(rewards[days - 1])


def game_signin(access_token, role_id, game_id):
    def _state_of(gid):
        try:
            return get_game_sign_state(access_token, gid)
        except Exception as ex:
            logging.warning(f'查询游戏签到状态失败(gameId={gid})：{ex}')
            return None

    def _reward_suffix(gid, state_data=None):
        try:
            reward_text = _today_reward_text(access_token, role_id, gid, state_data=state_data)
            return f'，今日道具：{reward_text}' if reward_text else ''
        except Exception as ex:
            logging.warning(f'读取游戏签到道具失败(gameId={gid}, roleId={role_id})：{ex}')
            return ''

    headers = {
        **REQUEST_HEADERS_BASE,
        'authorization': access_token,
        'appversion': APPVERSION,
        'User-Agent': OKHTTP_UA,
    }

    errors = []
    for sign_game_id in _candidate_sign_game_ids(game_id):
        response = _request_form(GAME_SIGNIN_URL, {'roleId': role_id, 'gameId': sign_game_id}, headers)
        resp = _safe_json(response, f'游戏签到(gameId={sign_game_id})')
        if _is_ok(resp):
            return True, f'签到成功（gameId={sign_game_id}）{_reward_suffix(sign_game_id)}'

        msg = str(resp.get('msg') or resp.get('message') or resp)
        if _is_already_signed(msg):
            state_data = _state_of(sign_game_id)
            if state_data and bool(state_data.get('todaySign')):
                return True, f'今日已签到（gameId={sign_game_id}）{_reward_suffix(sign_game_id, state_data=state_data)}'
            errors.append(f'gameId={sign_game_id} 返回“{msg}”但状态未签到')
            continue

        errors.append(f'gameId={sign_game_id}: {msg}')

    return False, '；'.join(errors) if errors else '游戏签到失败'


def _build_account_from_user_center(user_center, device_id):
    account = {
        'refreshToken': user_center['refreshToken'],
        'uid': str(user_center.get('uid', '')),
        'deviceId': device_id,
        'gameId': _default_game_id(),
        'roleIds': [],
    }
    try:
        account['roleIds'] = get_game_role_ids(
            user_center['accessToken'],
            account['uid'],
            device_id,
            account['gameId'],
        )
    except Exception as ex:
        print(f'自动获取角色ID失败：{ex}')
    return account


def send_login_captcha(phone, device_id=None):
    phone = str(phone or '').strip()
    if not phone:
        raise Exception('手机号不能为空')
    device_id = str(device_id or '').strip() or _random_device_id()
    send_captcha(phone, device_id)
    return device_id


def build_account_by_sms(phone, code, device_id):
    phone = str(phone or '').strip()
    code = str(code or '').strip()
    device_id = str(device_id or '').strip()
    if not phone:
        raise Exception('手机号不能为空')
    if not code:
        raise Exception('验证码不能为空')
    if not device_id:
        raise Exception('deviceId 不能为空，请先发送验证码')

    check_captcha(phone, code, device_id)
    token, user_id = login(phone, code, device_id)
    user_center = user_center_login(token, user_id, device_id)
    return _build_account_from_user_center(user_center, device_id)


def build_account_by_password(phone, password, device_id=None):
    phone = str(phone or '').strip()
    password = str(password or '').strip()
    if not phone:
        raise Exception('手机号不能为空')
    if not password:
        raise Exception('密码不能为空')

    device_id = str(device_id or '').strip() or _random_device_id()
    token, user_id = login_with_password(phone, password, device_id)
    user_center = user_center_login(token, user_id, device_id)
    return _build_account_from_user_center(user_center, device_id)


def login_by_code():
    phone = input('请输入手机号码：').strip()
    device_id = send_login_captcha(phone)
    code = input('请输入手机验证码：').strip()
    return build_account_by_sms(phone, code, device_id)


def login_by_password():
    phone = input('请输入手机号码：').strip()
    password = input('请输入账号密码：').strip()
    return build_account_by_password(phone, password)


def input_refresh_token():
    refresh_token = input('请输入 refreshToken：').strip()
    if not refresh_token:
        raise Exception('refreshToken 不能为空')

    uid = input('可选：请输入 uid（留空自动处理）：').strip()
    device_id = input('可选：请输入 deviceId（留空自动生成）：').strip() or _random_device_id()
    role_ids = _parse_role_ids(input('可选：请输入 roleId（多个用逗号分隔，留空自动获取）：').strip())
    return {
        'refreshToken': refresh_token,
        'uid': uid,
        'deviceId': device_id,
        'gameId': _default_game_id(),
        'roleIds': role_ids,
    }


def input_for_token():
    print('请输入你需要做什么：')
    print('1. 使用手机号+验证码登录（推荐）')
    print('2. 使用手机号+密码登录')
    print('3. 手动输入 refreshToken（高级）')
    mode = input('请输入（1，2，3）：').strip()
    if mode == '' or mode == '1':
        return login_by_code()
    if mode == '2':
        return login_by_password()
    if mode == '3':
        return input_refresh_token()
    raise SystemExit(-1)


def init_token():
    if token_env:
        print('使用环境变量里的账号信息...')
        return read_from_env()

    accounts = read(token_save_name)
    add_account = current_type == 'add_account'
    if add_account:
        print('！！！您启用了添加账号模式，将不会执行签到！！！')
    if len(accounts) == 0 or add_account:
        accounts.append(input_for_token())
    save(accounts)
    if add_account:
        return []

    selected_accounts = _select_accounts(accounts)
    print(f'本次将签到 {len(selected_accounts)} 个账号。')
    return selected_accounts


def do_sign(account):
    account['gameId'] = str(account.get('gameId') or _default_game_id())
    if not account.get('deviceId'):
        account['deviceId'] = _random_device_id()
    access_token = refresh_access_token(account)

    success = True
    uid = str(account.get('uid') or '').strip()
    if uid:
        app_ok, app_msg = app_signin(access_token, uid, account['deviceId'])
        account_msg = f'账号{uid}：{app_msg}'
        print(account_msg)
        if app_ok:
            logging.info(account_msg)
        else:
            logging.warning(account_msg)
        if not app_ok:
            success = False
    else:
        skip_msg = '当前账号没有 uid，跳过社区签到。'
        print(skip_msg)
        logging.info(skip_msg)

    role_ids = _dedup_list(account.get('roleIds', []))
    env_role_ids = _parse_role_ids(role_ids_env)
    if env_role_ids:
        role_ids = _dedup_list(role_ids + env_role_ids)
    if not role_ids and uid:
        role_ids = get_game_role_ids(access_token, uid, account['deviceId'], account['gameId'])
    account['roleIds'] = role_ids

    if not role_ids:
        print('未找到角色ID，请设置 TGD_ROLE_IDS 或重新登录以自动拉取角色。')
        return False

    for role_id in role_ids:
        ok, message = game_signin(access_token, role_id, account['gameId'])
        if ok:
            role_msg = f'角色{role_id}签到成功：{message}'
            print(role_msg)
            logging.info(role_msg)
        else:
            role_msg = f'角色{role_id}签到失败：{message}'
            print(role_msg)
            logging.warning(role_msg)
            success = False
    return success


def start():
    try:
        accounts = init_token()
    except Exception as ex:
        print(f'初始化失败，原因：{str(ex)}')
        logging.error('', exc_info=ex)
        return False

    success = True
    for account in accounts:
        try:
            success = do_sign(account) and success
        except Exception as ex:
            print(f'签到失败，原因：{str(ex)}')
            logging.error('', exc_info=ex)
            success = False

    if accounts and not token_env:
        save(accounts)
    print('签到完成！')
    return success


if __name__ == '__main__':
    print('塔吉多（异环）自动签到脚本')
    config_logger()

    logging.info('任务开始')

    start_time = time.time()
    success = start()
    end_time = time.time()
    logging.info(f'任务结束 | success={success} | 耗时={(end_time - start_time) * 1000:.0f}ms')
    if (exit_when_fail_env == "on") and not success:
        exit(1)
    if (os.name == 'nt') and (not token_env) and (not no_pause_env) and (not success):
        input('运行失败，按回车键退出...')
