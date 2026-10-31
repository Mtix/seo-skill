#!/usr/bin/env python3
"""Interactive local connection setup: Google Search Console and Yandex Webmaster."""
import argparse
from datetime import datetime, timezone
import getpass
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener

from wordstat import load_env, NoRedirect, WordstatError

SCOPE = 'https://www.googleapis.com/auth/webmasters.readonly'
TOKEN_URI = 'https://oauth2.googleapis.com/token'
AUTH_URI = 'https://accounts.google.com/o/oauth2/auth'

class SetupError(Exception):
    pass


def private_dir(path):
    if path.is_symlink():
        raise SetupError('Secret directory must not be a symlink')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def write_private(path, data):
    private_dir(path.parent)
    if path.is_symlink():
        raise SetupError('Secret file must not be a symlink')
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def read_json(path):
    if path.is_symlink():
        raise SetupError('Credential/config files must not be symlinks')
    return json.loads(path.read_text(encoding='utf-8'))


def ask_choice(labels, prompt):
    for i, label in enumerate(labels, 1):
        print(f'{i}. {label}')
    while True:
        value = input(prompt).strip()
        if value.isdigit() and 1 <= int(value) <= len(labels):
            return int(value) - 1
        print('Введите номер из списка. Ctrl+C — отмена.')


def api_get(url, authorization):
    allowed = ('https://www.googleapis.com/webmasters/v3/sites', 'https://api.webmaster.yandex.net/v4/user')
    if not any(url == base or url.startswith(base + '/') for base in allowed):
        raise SetupError('Unsupported API destination')
    try:
        request = Request(url, headers={'Authorization': authorization, 'Accept': 'application/json'})
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            data = json.loads(response.read())
        if not isinstance(data, dict) or 'error' in data or 'error_code' in data:
            raise SetupError('API вернул неожиданный ответ')
        return data
    except HTTPError as error:
        raise SetupError(f'API HTTP {error.code}. Проверьте доступы, включение API и срок действия токена.') from None
    except (URLError, TimeoutError):
        raise SetupError('Сетевая ошибка. Настройка не подтверждена; автоматических повторов нет.') from None


def choose_property(entries, requested, url_field):
    if not entries:
        raise SetupError('Нет доступных подтверждённых сайтов. Проверьте аккаунт и права в сервисе.')
    if requested:
        matches = [entry for entry in entries if entry[url_field] == requested]
        if not matches:
            raise SetupError('Указанный ресурс отсутствует среди доступных сайтов. Используйте точный URL или sc-domain:domain.')
        return matches[0]
    return entries[ask_choice([entry[url_field] for entry in entries], 'Выберите сайт: ')]


def google_credentials(secret_dir, client_file=None, reauth=False):
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GoogleRequest
    except ImportError:
        raise SetupError('Установите зависимости: python3 -m pip install -r <skill>/requirements.txt') from None
    token_file = secret_dir / 'google-token.json'
    try:
        if token_file.exists() and not reauth:
            data = read_json(token_file)
            if data.get('token_uri') != TOKEN_URI or set(data.get('scopes', [])) != {SCOPE}:
                raise SetupError('Неверный endpoint или scope в сохранённых credentials. Выполните init --reauth.')
            creds = Credentials.from_authorized_user_info(data, scopes=[SCOPE])
            if not creds.valid:
                if not creds.refresh_token:
                    raise SetupError('Нет refresh token; выполните init --reauth.')
                creds.refresh(GoogleRequest())
        else:
            if not client_file:
                client_file = Path(input('Путь к скачанному OAuth JSON (Desktop app): ').strip()).expanduser()
            data = read_json(client_file)
            installed = data.get('installed', {})
            if installed.get('auth_uri') != AUTH_URI or installed.get('token_uri') != TOKEN_URI or not installed.get('client_id'):
                raise SetupError('Нужен официальный Google OAuth JSON типа Desktop app с endpoints Google.')
            flow = InstalledAppFlow.from_client_config(data, scopes=[SCOPE], autogenerate_code_verifier=True)
            print('Откроется системный браузер. Разрешите только чтение Search Console; ожидание до 3 минут.')
            logger = logging.getLogger('google_auth_oauthlib.flow')
            was_disabled = logger.disabled
            logger.disabled = True  # Callback access logs may contain the authorization code.
            try:
                creds = flow.run_local_server(host='127.0.0.1', port=0, open_browser=True,
                                              authorization_prompt_message=None,
                                              success_message='Доступ получен. Вернитесь в терминал.',
                                              timeout_seconds=180, access_type='offline', prompt='consent')
            finally:
                logger.disabled = was_disabled
        if not creds.valid or not creds.refresh_token:
            raise SetupError('Не получен доступ для повторных запусков. Повторите init --reauth.')
        if creds.granted_scopes is not None and set(creds.granted_scopes) != {SCOPE}:
            raise SetupError('Разрешённые scopes отличаются от ожидаемого read-only доступа.')
        return creds
    except SetupError:
        raise
    except Exception:
        # OAuth exceptions can include codes, tokens, callback URLs, client secrets.
        raise SetupError('Google авторизация не завершена. Проверьте Desktop client, тестового пользователя, согласие и срок токена. Секреты скрыты.') from None


def connect_google(secret_dir, client_file, requested, reauth):
    creds = google_credentials(secret_dir, client_file, reauth)
    entries = api_get('https://www.googleapis.com/webmasters/v3/sites', 'Bearer ' + creds.token).get('siteEntry', [])
    entries = [e for e in entries if e.get('permissionLevel') in ('siteOwner', 'siteFullUser', 'siteRestrictedUser')]
    entry = choose_property(entries, requested, 'siteUrl')
    write_private(secret_dir / 'google-token.json', json.loads(creds.to_json()))
    return dict(property=entry['siteUrl'], permission=entry['permissionLevel'], checked_at=datetime.now(timezone.utc).isoformat())


def connect_yandex(secret_dir, env_file, requested, reauth):
    token_file = secret_dir / 'yandex-token.json'
    token = load_env(env_file).get('YANDEX_WEBMASTER_TOKEN', '').strip()
    if not reauth and not token and token_file.exists():
        token = read_json(token_file).get('token', '')
    if reauth or not token:
        if not sys.stdin.isatty():
            raise SetupError('Для скрытого ввода токена нужен локальный терминал; либо заполните YANDEX_WEBMASTER_TOKEN в .env.')
        print('Получите OAuth токен с webmaster:hostinfo по инструкции references/setup.md. Ввод скрыт.')
        token = getpass.getpass('OAuth токен Яндекс Вебмастера: ').strip()
    if not token or any(c.isspace() for c in token):
        raise SetupError('Неверный формат токена Яндекса')
    user = api_get('https://api.webmaster.yandex.net/v4/user', 'OAuth ' + token)
    uid = str(user.get('user_id', ''))
    if not uid.isdigit():
        raise SetupError('Не получен ID пользователя Яндекса')
    hosts = api_get('https://api.webmaster.yandex.net/v4/user/' + quote(uid, safe='') + '/hosts', 'OAuth ' + token).get('hosts', [])
    entry = choose_property([h for h in hosts if h.get('verified') is True], requested, 'ascii_host_url')
    write_private(token_file, {'token': token})
    return dict(property=entry['ascii_host_url'], host_id=entry['host_id'], user_id=uid, checked_at=datetime.now(timezone.utc).isoformat())


def prepare(project):
    # Reject symlinked storage before writing credentials.
    root = project / '.seo'
    private_dir(root)
    secret_dir = root / 'secrets'
    private_dir(secret_dir)
    ignore = project / '.gitignore'
    if ignore.is_symlink():
        raise SetupError('.gitignore must not be a symlink')
    text = ignore.read_text() if ignore.exists() else ''
    if '/.seo/' not in text.splitlines():
        with ignore.open('a') as stream:
            stream.write(('\n' if text and not text.endswith('\n') else '') + '/.seo/\n')
    return root, secret_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'status'])
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--provider', choices=['google', 'yandex', 'both'])
    parser.add_argument('--google-client', type=Path)
    parser.add_argument('--google-property')
    parser.add_argument('--yandex-property')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--reauth', action='store_true')
    args = parser.parse_args(argv)
    project = args.project.expanduser().resolve()
    config_path = project / '.seo/connections.json'
    config = read_json(config_path) if config_path.exists() else {'schema_version': 1, 'connections': {}}
    if args.command == 'status':
        for provider in ('google', 'yandex'):
            entry = config['connections'].get(provider)
            print(f'{provider}: ' + (f"сохранён {entry['property']}; проверено {entry['checked_at']} (не проверялось сейчас)" if entry else 'не настроен'))
        return
    provider = args.provider
    if not provider:
        provider = ['google', 'yandex', 'both'][ask_choice(['Google Search Console', 'Яндекс Вебмастер', 'Оба сервиса'], 'Что подключить: ')]
    root, secret_dir = prepare(project)
    failed = False
    for name in (['google', 'yandex'] if provider == 'both' else [provider]):
        try:
            if name == 'google':
                entry = connect_google(secret_dir, args.google_client, args.google_property, args.reauth)
            else:
                entry = connect_yandex(secret_dir, args.env_file or project / '.env', args.yandex_property, args.reauth)
            config['connections'][name] = entry
            write_private(config_path, config)
            print(f"{name}: доступ проверен, выбран {entry['property']}")
        except SetupError as error:
            failed = True
            print(f'{name}: {error}', file=sys.stderr)
            if provider != 'both':
                raise SystemExit(1)
            print('Остальные подключения сохраняются. Этот сервис можно настроить повторным init.', file=sys.stderr)
    print(f'Настройки: {config_path}. Секреты: {secret_dir} (локально, без шифрования).')
    if failed:
        raise SystemExit(1)

if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\nНастройка отменена; ранее сохранённые подключения оставлены.', file=sys.stderr)
        sys.exit(1)
    except (SetupError, WordstatError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('Настройка не завершена: ошибка конфигурации, файлов или сети. Подробности скрыты для защиты секретов.', file=sys.stderr)
        sys.exit(1)
