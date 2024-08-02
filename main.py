import json
import logging
import os
import random
import zoneinfo
from datetime import datetime, timedelta
from pprint import pformat

import google.cloud.logging
import requests
from google.cloud import firestore

# 標準 Logger の設定
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.DEBUG
)
logger = logging.getLogger()

# Cloud Logging ハンドラを logger に接続
logging_client = google.cloud.logging.Client()
logging_client.setup_logging()

# setup_logging() するとログレベルが INFO になるので DEBUG に変更
logger.setLevel(logging.DEBUG)


WEBHOOK_URL = os.environ['WEBHOOK_URL']
ICON_IMAGE_URL = os.environ['ICON_IMAGE_URL']
TWITCH_CLIENT_ID = os.environ['TWITCH_CLIENT_ID']
TWITCH_CLIENT_SECRET = os.environ['TWITCH_CLIENT_SECRET']
TWITCH_API_URL = 'https://api.twitch.tv'
JST = zoneinfo.ZoneInfo('Asia/Tokyo')

firestore_client = firestore.Client()


def get_firestore_twitch_token():
    logger.info('----- get firestore twitch token -----')
    doc_ref = firestore_client.collection('secretary_bot_v2').document('twitch')
    twitch_oauth_access_token = doc_ref.get().to_dict()['oauth_access_token']
    twitch_oauth_refresh_token = doc_ref.get().to_dict()['oauth_refresh_token']
    return twitch_oauth_access_token, twitch_oauth_refresh_token


def validate_twitch_access_token():
    logger.info('===== START validate twitch access token =====')
    twitch_oauth_token = get_firestore_twitch_token()
    twitch_oauth_access_token = twitch_oauth_token[0]
    twitch_oauth_refresh_token = twitch_oauth_token[1]

    logger.info('----- GET twitch api validate access token -----')
    headers = {
        'Authorization': f'Bearer {twitch_oauth_access_token}'
    }
    response = requests.get('https://id.twitch.tv/oauth2/validate', headers=headers)
    logger.info(f'response={response.text}')

    if response.status_code == 401:
        logger.info('----- POST twitch api refresh access token -----')
        response = requests.post(
            f'https://id.twitch.tv/oauth2/token?client_id={TWITCH_CLIENT_ID}&client_secret={TWITCH_CLIENT_SECRET}'
            f'&grant_type=refresh_token&refresh_token={twitch_oauth_refresh_token}',
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        ).json()
        logger.info(f'response={response}')

        logger.info('----- update firestore twitch token -----')
        token = {
            'oauth_access_token': response['access_token'],
            'oauth_refresh_token': response['refresh_token']
        }
        firestore_client.collection('secretary_bot_v2').document('twitch').set(token)

    logger.info('===== END validate twitch access token =====')
    return [twitch_oauth_access_token, twitch_oauth_refresh_token]


def twitch_api_header(twitch_token):
    headers = {
        'Authorization': f'Bearer {twitch_token}',
        'Client-Id': TWITCH_CLIENT_ID,
        'Content-Type': 'application/json',
    }
    return headers


def get_twitch_user_info(twitch_oauth_access_token, twitch_broadcaster_user_id):
    logger.info('----- GET twitch api get user info -----')
    headers = twitch_api_header(twitch_oauth_access_token)
    user_info = requests.get(f'{TWITCH_API_URL}/helix/users?id={twitch_broadcaster_user_id}', headers=headers).json()
    logger.debug(f'response={user_info}')
    return user_info


def get_channel_info(twitch_oauth_access_token, twitch_broadcaster_user_id):
    logger.info('----- GET twitch api get channel info -----')
    headers = twitch_api_header(twitch_oauth_access_token)
    channel_info = requests.get(
        f'{TWITCH_API_URL}/helix/channels?broadcaster_id={twitch_broadcaster_user_id}',
        headers=headers
    ).json()
    logger.debug(f'response={channel_info}')
    return channel_info


def event_subscription_handler(request):
    logger.info('===== START event subscription handler =====')
    request_json = request.get_json()
    logger.debug(f'request={request_json}')

    try:
        twitch_subscription_type = request_json["subscription"]["type"]
        logger.debug(f'subscription_type={twitch_subscription_type}')

        # Twitchトークン更新＆取得
        twitch_oauth_access_token = validate_twitch_access_token()[0]

        logger.info('----- check message type -----')
        logger.debug(f'twitch-eventsub-message-type={request.headers["Twitch-Eventsub-Message-Type"]}')
        message_type_notification = 'notification'
        message_type_verification = 'webhook_callback_verification'

        # 配信開始、または更新したら通知
        if request.headers['Twitch-Eventsub-Message-Type'] == message_type_notification:
            twitch_broadcaster_user_id = request_json["event"]["broadcaster_user_id"]
            twitch_broadcaster_user_login = request_json['event']['broadcaster_user_login']

            user_info = get_twitch_user_info(twitch_oauth_access_token, twitch_broadcaster_user_id)
            channel_info = get_channel_info(twitch_oauth_access_token, twitch_broadcaster_user_id)

            color = random.randint(0, 16777215)
            twitch_broadcaster_user_name = request_json['event']['broadcaster_user_name']
            twitch_channel_title = channel_info['data'][0]['title']
            twitch_game_name = channel_info['data'][0]['game_name']
            twitch_stream_title = channel_info['data'][0]['title']
            twitch_profile_image_url = user_info['data'][0]['profile_image_url']

            logger.debug('----- create post content -----')
            if request_json['subscription']['type'] == 'stream.online':
                headers = {'Content-Type': 'application/json'}
                content = f'{twitch_broadcaster_user_name}さんがライブ配信中です！ {twitch_game_name} : {twitch_stream_title}'
                embeds = [
                    {
                        'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
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
                        'thumbnail': {
                            'url': twitch_profile_image_url,
                        },
                        'fields': [
                            {
                                'name': 'Streamer Name',
                                'value': ''
                            },
                            {
                                'name': 'Title',
                                'value': f'[{twitch_channel_title}](https://www.twitch.tv/{twitch_broadcaster_user_login}'
                            },
                            {
                                'name': 'Playing',
                                'value': twitch_game_name,
                                'inline': True
                            },
                            {
                                'name': 'Start at',
                                'value': str(datetime.strptime(request_json['event']['started_at'], '%Y-%m-%dT%H:%M:%SZ') + timedelta(hours=9)),
                                'inline': True
                            },
                        ]
                    },
                ]

                body = {
                    'username': 'Twitch Stream Notifier',
                    'avatar_url': ICON_IMAGE_URL,
                    'content': content,
                    'embeds': embeds
                }

                logger.debug(f'webhook_url={WEBHOOK_URL}')
                logger.debug(f'headers={pformat(headers)}')
                logger.debug(f'body={pformat(body)}')

                logger.info('----- post message -----')
                response = requests.post(WEBHOOK_URL, json.dumps(body), headers=headers)

                logger.debug(f'response.status={pformat(response.status_code)}')
                return 'event subscription success!', 204

            # コールバックリクエストなら
            elif request.headers['Twitch-Eventsub-Message-Type'] == message_type_verification:
                headers = {'Content-Type': 'application/json'}
                content = f'{twitch_broadcaster_user_name}さんの{twitch_subscription_type}イベントのサブスクリプションが成功しました!>'

                body = {
                    'username': 'Twitch Stream Notifier',
                    'avatar_url': ICON_IMAGE_URL,
                    'content': content
                }

                logger.debug(f'webhook_url={WEBHOOK_URL}')
                logger.debug(f'headers={pformat(headers)}')
                logger.debug(f'body={pformat(body)}')

                logger.info('----- post message -----')
                response = requests.post(WEBHOOK_URL, json.dumps(body), headers=headers)

                logger.debug(f'response.status={pformat(response.status_code)}')
                return request_json['challenge'], 200

    except Exception as e:
        logger.exception(e)
    finally:
        logger.info('===== END event subscription handler =====')
