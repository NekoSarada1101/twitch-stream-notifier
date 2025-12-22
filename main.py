"""
Twitch EventSub Webhook Handler

TwitchのEventSubからの通知を受け取り、Discord等のWebhookへ通知を行うCloud Function。
Firestoreを使用してTwitchのOAuthトークンを管理し、必要に応じてリフレッシュを行う。
"""

import json
import logging
import os
import random
import zoneinfo
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple, Literal

import google.cloud.logging
from google.cloud import firestore
import requests

# --- Configuration & Constants ---

# 環境変数の読み込み（存在しない場合は即座にエラーとする）
try:
    WEBHOOK_URL = os.environ['WEBHOOK_URL']
    ICON_IMAGE_URL = os.environ['ICON_IMAGE_URL']
    TWITCH_CLIENT_ID = os.environ['TWITCH_CLIENT_ID']
    TWITCH_CLIENT_SECRET = os.environ['TWITCH_CLIENT_SECRET']
except KeyError as e:
    raise EnvironmentError(f"必須の環境変数が設定されていません: {e}")

TWITCH_AUTH_URL = 'https://id.twitch.tv/oauth2'
TWITCH_API_URL = 'https://api.twitch.tv'
JST = zoneinfo.ZoneInfo('Asia/Tokyo')
FIRESTORE_COLLECTION = 'secretary_bot_v2'
FIRESTORE_DOC = 'twitch'
REQUEST_TIMEOUT = 10.0  # 秒

# --- Logging Setup ---

# Cloud Loggingクライアントのセットアップ
logging_client = google.cloud.logging.Client()
logging_client.setup_logging()

# 標準ロガーの設定
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # 必要に応じてINFOに変更推奨


# --- Classes ---

class FirestoreTokenRepository:
    """Firestoreを用いたTwitchトークンの永続化・取得を担当するクラス"""

    def __init__(self, client: firestore.Client):
        self.client = client
        self.doc_ref = self.client.collection(FIRESTORE_COLLECTION).document(FIRESTORE_DOC)

    def get_tokens(self) -> Tuple[str, str]:
        """
        Firestoreからアクセストークンとリフレッシュトークンを取得する。

        Returns:
            Tuple[str, str]: (access_token, refresh_token)

        Raises:
            KeyError: ドキュメントに必要なフィールドが存在しない場合
        """
        doc = self.doc_ref.get()
        if not doc.exists:
            logger.error("Firestore document not found.")
            raise FileNotFoundError(f"Document {FIRESTORE_DOC} not found.")

        data = doc.to_dict()
        if not data:
            raise ValueError("Firestore document is empty.")

        return data['oauth_access_token'], data['oauth_refresh_token']

    def save_tokens(self, access_token: str, refresh_token: str) -> None:
        """
        更新されたトークンをFirestoreに保存する。

        Args:
            access_token (str): 新しいアクセストークン
            refresh_token (str): 新しいリフレッシュトークン
        """
        logger.info("Updating Twitch tokens in Firestore.")
        self.doc_ref.set({
            'oauth_access_token': access_token,
            'oauth_refresh_token': refresh_token
        }, merge=True)


class TwitchClient:
    """Twitch APIとの通信及び認証ロジックを担当するクラス"""

    def __init__(self, repository: FirestoreTokenRepository):
        self.repository = repository
        self.access_token, self.refresh_token = self.repository.get_tokens()

    def _get_headers(self) -> Dict[str, str]:
        return {
            'Authorization': f'Bearer {self.access_token}',
            'Client-Id': TWITCH_CLIENT_ID,
            'Content-Type': 'application/json',
        }

    def _refresh_access_token(self) -> None:
        """リフレッシュトークンを使用してアクセストークンを再取得し、保存する"""
        logger.info("Attempting to refresh Twitch access token.")
        payload = {
            'client_id': TWITCH_CLIENT_ID,
            'client_secret': TWITCH_CLIENT_SECRET,
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token
        }

        try:
            response = requests.post(
                f'{TWITCH_AUTH_URL}/token',
                data=payload,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()

            self.access_token = data['access_token']
            self.refresh_token = data['refresh_token']
            self.repository.save_tokens(self.access_token, self.refresh_token)
            logger.info("Token refresh successful.")

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to refresh token: {e}", exc_info=True)
            raise

    def validate_and_ensure_token(self) -> None:
        """
        トークンの有効性を確認し、無効であればリフレッシュを行う。
        """
        logger.debug("Validating Twitch access token.")
        headers = {'Authorization': f'Bearer {self.access_token}'}

        try:
            response = requests.get(
                f'{TWITCH_AUTH_URL}/validate',
                headers=headers,
                timeout=REQUEST_TIMEOUT
            )

            if response.status_code == 401:
                logger.warning("Token invalid (401). Refreshing...")
                self._refresh_access_token()
            elif response.status_code != 200:
                logger.warning(f"Unexpected status code during validation: {response.status_code}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Error during token validation: {e}")
            # 検証エラーでも一旦リフレッシュを試みるなどの戦略も考えられるが、ここではログ出力のみ

    def get_user_info(self, user_id: str) -> Dict[str, Any]:
        """Twitchユーザー情報を取得する"""
        return self._make_request('GET', f'{TWITCH_API_URL}/helix/users', params={'id': user_id})

    def get_channel_info(self, broadcaster_id: str) -> Dict[str, Any]:
        """チャンネル情報を取得する"""
        return self._make_request('GET', f'{TWITCH_API_URL}/helix/channels', params={'broadcaster_id': broadcaster_id})

    def get_streams(self, user_id: str) -> Dict[str, Any]:
        """配信情報を取得する"""
        return self._make_request('GET', f'{TWITCH_API_URL}/helix/streams', params={'user_id': user_id})

    def _make_request(self, method: str, url: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """
        APIリクエストの共通ハンドラ。401発生時の自動リトライ機能を含む。
        """
        try:
            headers = self._get_headers()
            response = requests.request(method, url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)

            if response.status_code == 401:
                logger.warning("Received 401 Unauthorized. Retrying after refresh.")
                self._refresh_access_token()
                headers = self._get_headers()  # Update headers with new token
                response = requests.request(method, url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)

            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Twitch API request failed: {url}, Error: {e}", exc_info=True)
            raise


class DiscordNotifier:
    """Discordへの通知ペイロード生成と送信を担当するクラス"""

    @staticmethod
    def send_notification(payload: Dict[str, Any]) -> int:
        """
        WebhookへJSONをPOSTする。

        Returns:
            int: HTTPステータスコード
        """
        logger.info("Sending notification to Webhook.")
        try:
            headers = {'Content-Type': 'application/json'}
            response = requests.post(WEBHOOK_URL, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
            logger.debug(f"Webhook response: {response.status_code}")
            return response.status_code
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send webhook: {e}", exc_info=True)
            return 500

    @staticmethod
    def create_stream_online_payload(
        user_name: str,
        user_login: str,
        game_name: str,
        title: str,
        profile_image_url: str,
        started_at_str: str
    ) -> Dict[str, Any]:
        """配信開始通知用のPayloadを作成"""

        # 色の生成 (ランダム)
        color = random.randint(0, 16777215)

        # 日本時間の開始時刻へ変換
        dt_started = datetime.strptime(started_at_str, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        start_time_jst = dt_started.astimezone(JST).strftime('%Y-%m-%d %H:%M:%S')

        content = f"{user_name}さんがライブ配信中です！ {game_name} : {title}"

        embed = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'color': color,
            'footer': {
                'text': 'Twitch Stream Notifier',
                'icon_url': ICON_IMAGE_URL
            },
            'author': {
                'name': '@Twitch',
                'url': 'https://www.twitch.tv/',
                'icon_url': ICON_IMAGE_URL
            },
            'thumbnail': {'url': profile_image_url},
            'fields': [
                {'name': 'Streamer Name', 'value': user_name},
                {'name': 'Title', 'value': f'[{title}](https://www.twitch.tv/{user_login})'},
                {'name': 'Playing', 'value': game_name, 'inline': True},
                {'name': 'Start at', 'value': start_time_jst, 'inline': True},
            ]
        }

        return {
            'username': 'Twitch Stream Notifier',
            'avatar_url': ICON_IMAGE_URL,
            'content': content,
            'embeds': [embed]
        }

    @staticmethod
    def create_channel_update_payload(
        user_name: str,
        user_login: str,
        game_name: str,
        title: str,
        profile_image_url: str
    ) -> Dict[str, Any]:
        """チャンネル情報更新通知用のPayloadを作成"""
        color = random.randint(0, 16777215)
        content = f"{user_name}さんの配信が更新されました。 {game_name} : {title}"

        embed = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'color': color,
            'footer': {'text': 'Twitch Stream Notifier', 'icon_url': ICON_IMAGE_URL},
            'author': {'name': '@Twitch', 'url': 'https://www.twitch.tv/', 'icon_url': ICON_IMAGE_URL},
            'thumbnail': {'url': profile_image_url},
            'fields': [
                {'name': 'Streamer Name', 'value': user_name},
                {'name': 'Title', 'value': f'[{title}](https://www.twitch.tv/{user_login})'},
                {'name': 'Playing', 'value': game_name, 'inline': True}
            ]
        }

        return {
            'username': 'Twitch Stream Notifier',
            'avatar_url': ICON_IMAGE_URL,
            'content': content,
            'embeds': [embed]
        }

    @staticmethod
    def create_verification_payload(display_name: str, sub_type: str) -> Dict[str, Any]:
        """検証成功通知用のPayloadを作成"""
        return {
            'username': 'Twitch Stream Notifier',
            'avatar_url': ICON_IMAGE_URL,
            'content': f'{display_name}さんの{sub_type}イベントのサブスクリプションが成功しました!'
        }


# --- Global Instances (Warm Start用) ---
# Cloud Functionsではグローバルスコープの変数はリクエスト間で再利用されるため
firestore_client = firestore.Client()
token_repo = FirestoreTokenRepository(firestore_client)
# TwitchClientは内部でToken状態を持つため、リクエストごとに生成するか、
# トークン更新ロジックを頑健にする必要がある。ここではリクエスト毎にvalidateするためインスタンス化しておく。
twitch_client = TwitchClient(token_repo)


# --- Main Handler ---

def event_subscription_handler(request) -> Tuple[str, int]:
    """
    Cloud Functions エントリーポイント
    """
    logger.info("===== START event subscription handler =====")

    try:
        request_json = request.get_json()
        if not request_json:
            logger.warning("Request JSON is empty.")
            return 'Bad Request', 400

        # ヘッダー情報の取得
        msg_type = request.headers.get('Twitch-Eventsub-Message-Type')
        if not msg_type:
            logger.warning("Missing Twitch-Eventsub-Message-Type header.")
            return 'Missing Header', 400

        logger.info(f"Message Type: {msg_type}")

        # トークンの有効性確認 (必要ならリフレッシュ)
        twitch_client.validate_and_ensure_token()

        # 1. Verification (コールバック確認)
        if msg_type == 'webhook_callback_verification':
            return _handle_verification(request_json)

        # 2. Notification (通知イベント)
        elif msg_type == 'notification':
            return _handle_notification(request_json)

        else:
            logger.info(f"Unknown message type: {msg_type}")
            return 'ok', 200

    except Exception as e:
        # 予期せぬエラーはスタックトレースと共にログ出力するが、
        # Twitch側へのリトライ爆撃を防ぐために 204 or 200 を返すのが通例
        logger.error(f"Unhandled exception in handler: {e}", exc_info=True)
        return 'Internal Server Error (Handled)', 204
    finally:
        logger.info("===== END event subscription handler =====")


def _handle_verification(request_json: Dict[str, Any]) -> Tuple[str, int]:
    """Verificationイベントの処理"""
    challenge = request_json.get('challenge')
    broadcaster_id = request_json["subscription"]["condition"]["broadcaster_user_id"]
    sub_type = request_json["subscription"]["type"]

    # ユーザー名取得
    user_info = twitch_client.get_user_info(broadcaster_id)
    if user_info and user_info.get('data'):
        display_name = user_info['data'][0]['display_name']
        payload = DiscordNotifier.create_verification_payload(display_name, sub_type)
        DiscordNotifier.send_notification(payload)

    return challenge, 200


def _handle_notification(request_json: Dict[str, Any]) -> Tuple[str, int]:
    """Notificationイベントの処理"""
    event = request_json.get('event', {})
    sub_type = request_json.get('subscription', {}).get('type')

    broadcaster_id = event.get("broadcaster_user_id")
    user_name = event.get('broadcaster_user_name')
    user_login = event.get('broadcaster_user_login')

    # APIから最新情報を取得
    channel_info_resp = twitch_client.get_channel_info(broadcaster_id)
    user_info_resp = twitch_client.get_user_info(broadcaster_id)

    if not (channel_info_resp.get('data') and user_info_resp.get('data')):
        logger.warning("Failed to fetch channel or user info from Twitch API.")
        return 'ok', 204

    channel_data = channel_info_resp['data'][0]
    user_data = user_info_resp['data'][0]

    game_name = channel_data['game_name']
    stream_title = channel_data['title']
    profile_image_url = user_data['profile_image_url']

    payload = None

    if sub_type == 'stream.online':
        started_at = event.get('started_at')
        payload = DiscordNotifier.create_stream_online_payload(
            user_name, user_login, game_name, stream_title, profile_image_url, started_at
        )

    elif sub_type == 'channel.update':
        # 配信中かどうかのチェック
        streams_resp = twitch_client.get_streams(broadcaster_id)
        if not streams_resp.get('data'):
            logger.info(f"{user_name} is not streaming now. Skip update notification.")
            return 'end', 204

        payload = DiscordNotifier.create_channel_update_payload(
            user_name, user_login, game_name, stream_title, profile_image_url
        )

    if payload:
        DiscordNotifier.send_notification(payload)

    return 'end', 204
