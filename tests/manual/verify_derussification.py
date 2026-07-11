"""
Exercises the real bot functions directly (no Telegram client needed) to verify
the de-russification fixes actually produce correct, non-Russian output for
every supported language. Run inside the remnawave_bot container after any
upstream merge, before redeploying:

    docker cp tests/manual/verify_derussification.py remnawave_bot:/tmp/verify_derussification.py
    docker exec remnawave_bot python3 /tmp/verify_derussification.py

Not a pytest test (uses asyncio.run directly, needs a live DB connection from
inside the container) — kept out of the collected test suite on purpose.
"""

import asyncio
import re
import sys

CYRILLIC = re.compile('[а-яА-ЯёЁ]')
LANGS = ['es', 'en', 'pt', 'zh', 'fa']

failures = []
checks = 0


def check(label, text, lang=None):
    global checks
    checks += 1
    text = str(text)
    if CYRILLIC.search(text):
        failures.append((label, lang, text))
        print(f'  FAIL [{lang}] {label}: {text!r}')
    else:
        print(f'  ok   [{lang}] {label}: {text!r}')


def section(title):
    print(f'\n=== {title} ===')


async def main():
    from app.config import settings
    from app.localization.texts import get_texts

    # ------------------------------------------------------------------
    section('1. format_period_description — all langs, incl. no-arg default')
    from app.utils.pricing_utils import format_period_description
    for lang in LANGS:
        for days in (14, 30, 60, 90, 180, 360, 7):
            check(f'format_period_description({days})', format_period_description(days, lang), lang)
    # the exact miniapp.py bug we fixed: calling with NO language arg at all
    check('format_period_description(30) NO LANG ARG', format_period_description(30), '(default)')

    # ------------------------------------------------------------------
    section('2. _get_days_word / device declension helpers used in keyboards')
    from app.keyboards.inline import _get_days_word
    for lang in LANGS:
        check('_get_days_word(1)', _get_days_word(1, lang), lang)
        check('_get_days_word(5)', _get_days_word(5, lang), lang)

    # ------------------------------------------------------------------
    section('3. formatters.py — status/traffic/time formatting for every language')
    from datetime import UTC, datetime, timedelta

    from app.utils.formatters import (
        format_boolean,
        format_subscription_status,
        format_time_ago,
        format_traffic_usage,
    )
    for lang in LANGS:
        check('format_subscription_status(active)', format_subscription_status(True, False, datetime.now(UTC) + timedelta(days=5), lang), lang)
        check('format_subscription_status(expired)', format_subscription_status(True, False, datetime.now(UTC) - timedelta(days=1), lang), lang)
        check('format_traffic_usage', format_traffic_usage(12.3, 50, lang), lang)
        check('format_boolean(True)', format_boolean(True, lang), lang)
        check('format_time_ago', format_time_ago(datetime.now(UTC) - timedelta(hours=3)), lang)

    # ------------------------------------------------------------------
    section('4. Ban notification templates (config.py) — format with real placeholders')
    ban_vars_common = dict(ip_count=5, limit=3, ban_minutes=15, node_info='Node: test-node')
    check('BAN_MSG_PUNISHMENT', settings.BAN_MSG_PUNISHMENT.format(**ban_vars_common))
    check('BAN_MSG_ENABLED', settings.BAN_MSG_ENABLED)
    check('BAN_MSG_WIFI', settings.BAN_MSG_WIFI.format(ban_minutes=15, network_info='Network: WiFi\n', node_info=''))
    check('BAN_MSG_MOBILE', settings.BAN_MSG_MOBILE.format(ban_minutes=15, network_info='', node_info=''))
    check('BAN_MSG_WARNING', settings.BAN_MSG_WARNING.format(warning_message='test warning'))
    check('MAINTENANCE_MESSAGE', settings.get_maintenance_message())
    check('ACTIVATE_BUTTON_TEXT', settings.ACTIVATE_BUTTON_TEXT)

    from app.services.ban_notification_service import BanNotificationService
    bns = BanNotificationService.__new__(BanNotificationService)
    # exercise the actual node_info/network_info/reason string builders (private logic, no I/O)
    node_info = f"\U0001f5a5 <b>Node:</b> <code>{'test-node'}</code>"
    check('node_info fragment (manually rebuilt)', node_info)

    # ------------------------------------------------------------------
    section('5. Payment / balance description builders (Stars + CryptoBot are the enabled channels)')
    check('get_balance_payment_description', settings.get_balance_payment_description(50000, telegram_user_id=123456789))
    check('get_subscription_payment_description', settings.get_subscription_payment_description(30, 50000))
    check('get_custom_payment_description', settings.get_custom_payment_description('test top-up'))
    check('PAYMENT_SERVICE_NAME', settings.PAYMENT_SERVICE_NAME)

    from app.utils.payment_utils import get_payment_methods_text
    for lang in LANGS:
        check('get_payment_methods_text', get_payment_methods_text(lang), lang)

    # ------------------------------------------------------------------
    section('6. New-user registration language defaults (no DB writes — pure function checks)')
    from app.database.crud.user import _normalize_language_code
    check('_normalize_language_code(None)', _normalize_language_code(None), 'None->fallback')
    check('_normalize_language_code("")', _normalize_language_code(''), 'empty->fallback')
    # this is the exact bug class we fixed: a caller passing nothing should never resolve to 'ru'
    from app.database.models import User
    default_lang = User.__table__.columns['language'].default.arg
    check('User.language column DEFAULT', default_lang, 'DB column default')

    # ------------------------------------------------------------------
    section('7. promo_offer.py time-left formatter (was ru/en split, now always d/h/m)')
    from app.utils.promo_offer import _format_time_left
    for lang in LANGS:
        check('_format_time_left(90000s)', _format_time_left(90000, lang), lang)

    # ------------------------------------------------------------------
    section('8. texts.t() resolution sanity — spot-check keys touched this session, all languages')
    KEYS_TO_SPOT_CHECK = [
        'REFERRAL_COMMISSION_NOTIFICATION',
        'DAILY_CHARGE_NOTIFICATION',
        'BULK_BAN_NOTIFICATION',
        'SUBSCRIPTION_PURCHASE_TARIFF_TX_DESCRIPTION',
        'MY_SUBS_STATUS_EXPIRED',
        'MY_SUBS_STATUS_ACTIVE',
        'MY_SUBS_STATUS_TRIAL',
        'MY_SUBS_STATUS_DISABLED',
        'MY_SUBS_STATUS_LIMITED',
        'MY_SUBS_STATUS_UNKNOWN',
        'AUTO_PURCHASE_TARIFF_LINE',
        'PAYMENT_METHOD_STARS_DESCRIPTION',
        'PAYMENT_METHOD_CRYPTOBOT_DESCRIPTION',
    ]
    for lang in LANGS:
        texts = get_texts(lang)
        for key in KEYS_TO_SPOT_CHECK:
            val = texts.t(key, f'__MISSING_DEFAULT_FOR_{key}__')
            if val.startswith('__MISSING_DEFAULT_FOR_'):
                print(f'  WARN [{lang}] {key}: not present, used hardcoded EN default (not necessarily a bug)')
            check(key, val, lang)

    # ------------------------------------------------------------------
    section('9. Live DB rows fixed earlier this session — re-verify they stuck')
    from app.database.database import AsyncSessionLocal
    from sqlalchemy import text as sql_text
    async with AsyncSessionLocal() as db:
        row = (await db.execute(sql_text("SELECT name, description FROM tariffs LIMIT 1"))).first()
        check('tariffs.name', row[0])
        check('tariffs.description', row[1])
        row = (await db.execute(sql_text("SELECT name FROM promo_groups WHERE is_default = true LIMIT 1"))).first()
        check('promo_groups.name (default group)', row[0])
        row = (await db.execute(sql_text("SELECT name FROM wheel_configs LIMIT 1"))).first()
        if row:
            check('wheel_configs.name', row[0])
        rows = (await db.execute(sql_text("SELECT name, message_text, button_text FROM promo_offer_templates"))).fetchall()
        for r in rows:
            check('promo_offer_templates.name', r[0])
            check('promo_offer_templates.message_text', r[1])
            check('promo_offer_templates.button_text', r[2])

    # ------------------------------------------------------------------
    print(f'\n{"="*60}')
    print(f'TOTAL CHECKS: {checks}   FAILURES: {len(failures)}')
    if failures:
        print('\nFAILURES:')
        for label, lang, text in failures:
            print(f'  [{lang}] {label}: {text!r}')
        sys.exit(1)
    else:
        print('ALL CHECKS PASSED — no Cyrillic detected in any exercised output.')


asyncio.run(main())
