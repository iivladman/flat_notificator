import os
import json
import re
import sys
import time
import requests
import traceback
from bs4 import BeautifulSoup
from io import BytesIO
from PyPDF2 import PdfReader
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import cloudscraper

load_dotenv()

# --- Настройки Telegram ---
# Значения берутся из переменных окружения (в GitHub Actions это Secrets)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- Настройки Источника 1 (Звязда) ---
ZVIAZDA_URL = "https://zviazda.by/zviazda_pdf/"
ZVIAZDA_STATE_FILE = "processed_pdfs.json"
KEYWORD_REGEX = re.compile(r'проектн(?:ая|ой|ую|ые|ых)\s+деклараци(?:я|и|ю|й)', re.IGNORECASE)

# --- Настройки Источника 2 (УКС Запад) ---
UKS_ZAPAD_URL = "https://ukszapad.by/index.php/nashi-proekty/deklaratsii2.html"
UKS_ZAPAD_STATE_FILE = "uks_zapad_state.json"

# --- Настройки Источника 3 (МАПИД) ---
MAPID_URL = "https://mapid.by/nedvizhimost/realizaciya-kvartir.html"
MAPID_STATE_FILE = "mapid_state.json"

# --- Настройки Источника 4 (Минскстрой) ---
MINSKSTROY_URL = "https://minskstroy.by/ru/adsall"
MINSKSTROY_STATE_FILE = "minskstroy_state.json"


# --- Настройки Источника 5 (Арендное жилье Мингорисполкома) ---
MINSK_GOV_RENTAL_URL = "https://minsk.gov.by/ru/freepage/other/arendnoe_zhiljo/"
MINSK_GOV_RENTAL_STATE_FILE = "minsk_gov_rental_state.json"


# ==========================================
# БАЗОВЫЕ ФУНКЦИИ
# ==========================================

def get_robust_session():
    """Создает сессию, которая притворяется реальным браузером и обходит защиту от ботов."""
    
    # Вместо обычного requests.Session() используем cloudscraper
    session = cloudscraper.create_scraper(
        browser={
            'browser': 'chrome',
            'platform': 'windows',
            'desktop': True
        }
    )
    
    # Оставляем нашу логику повторных попыток
    retries = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    
    # Добавляем "человеческие" заголовки, чтобы сервер думал, что мы обычный пользователь
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0"
    })
    
    return session


def send_telegram_message(text):
    """Отправляет сообщение в Telegram через бота."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram бот не настроен. Задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.")
        print(f"[Текст уведомления: {text}]")
        return False
    
    try:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True # Чтобы ссылки на PDF не создавали огромные превью
        }
        response = requests.post(api_url, json=payload, timeout=10)
        response.raise_for_status()
        print("Telegram уведомление успешно отправлено.")
        return True
    except Exception as e:
        print(f"Ошибка при отправке в Telegram: {e}")
        traceback.print_exc()
        return False

# ==========================================
# ИСТОЧНИКИ ПАРСИНГА
# ==========================================

def check_zviazda_pdfs():
    """Источник 1: Проверка новых PDF-выпусков газеты 'Звязда'."""
    notifications = []
    
    # Загружаем уже проверенные ссылки
    processed_pdfs = set()
    if os.path.exists(ZVIAZDA_STATE_FILE):
        with open(ZVIAZDA_STATE_FILE, 'r', encoding='utf-8') as f:
            try:
                processed_pdfs = set(json.load(f))
            except json.JSONDecodeError:
                pass

    try:
        # Получаем ссылки на странице
        session = get_robust_session()
        # timeout=(10, 30) означает: 10 сек на подключение, 30 сек на скачивание страницы
        response = session.get(ZVIAZDA_URL, timeout=(10, 30))
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        pdf_links = []
        for link in soup.find_all('a', href=True):
            href = link['href']
            if href.lower().endswith('.pdf'):
                if not href.startswith('http'):
                    base_url = "https://zviazda.by"
                    href = base_url + href if href.startswith('/') else f"{base_url}/zviazda_pdf/{href}"
                pdf_links.append(href)
                
    except Exception as e:
        print(f"Ошибка при получении списка ссылок Звязда: {e}")
        return notifications

    # Фильтруем новые
    new_pdfs = [link for link in pdf_links if link not in processed_pdfs]
    
    if not new_pdfs:
        print("Звязда: Нет новых выпусков для проверки.")
        return notifications

    for pdf_url in new_pdfs:
        print(f"Звязда: Проверка {pdf_url}...")
        try:
            session = get_robust_session()
            res = session.get(pdf_url, timeout=(10, 60))
            res.raise_for_status()
            
            pdf_file = BytesIO(res.content)
            reader = PdfReader(pdf_file)
            
            for page_num, page in enumerate(reader.pages, 1):
                text = page.extract_text()
                if text and KEYWORD_REGEX.search(text):
                    # Если нашли, формируем текст уведомления
                    msg = (
                        f"📄 <b>Найдена проектная декларация!</b>\n\n"
                        f"<b>Источник:</b> Газета «Звязда»\n"
                        f"<b>Страница:</b> {page_num}\n"
                        f"<a href='{pdf_url}'>Смотреть PDF</a>"
                    )
                    notifications.append(msg)
                    break # Достаточно одного совпадения на выпуск
                    
        except Exception as e:
            print(f"Звязда: Ошибка при обработке PDF {pdf_url}: {e}")
            
        # Добавляем в обработанные в любом случае
        processed_pdfs.add(pdf_url)

    # Сохраняем обновленный список
    with open(ZVIAZDA_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(processed_pdfs), f, indent=4, ensure_ascii=False)
        
    return notifications

def check_uks_zapad():
    """Источник 2: Проверка новых ссылок в основном контенте на сайте УКС Запад."""
    notifications = []
    processed_links = set()
    
    if os.path.exists(UKS_ZAPAD_STATE_FILE):
        with open(UKS_ZAPAD_STATE_FILE, 'r', encoding='utf-8') as f:
            try:
                processed_links = set(json.load(f))
            except json.JSONDecodeError:
                pass

    is_first_run = not os.path.exists(UKS_ZAPAD_STATE_FILE)

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(UKS_ZAPAD_URL, headers=headers) 
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # --- ВАЖНОЕ ОБНОВЛЕНИЕ ---
        # Вырезаем шапку, навигацию, подвал и боковые панели, чтобы не собирать мусорные ссылки
        for tag in soup(['nav', 'header', 'footer', 'aside']):
            tag.decompose()
            
        # Вырезаем элементы, у которых в классе есть слова menu, sidebar и т.д.
        for tag in soup.find_all(lambda t: t.has_attr('class') and any(c in ['menu', 'sidebar', 'moduletable'] for c in t['class'])):
            tag.decompose()
        # -------------------------
        
        current_links = {}
        for link in soup.find_all('a', href=True):
            href = link['href']
            text = link.get_text(strip=True) or "Без названия"
            
            if len(text) > 1 and not href.startswith(('mailto:', '#', 'javascript:', 'tel:')):
                if not href.startswith('http'):
                    base_url = "https://ukszapad.by"
                    href = base_url + href if href.startswith('/') else f"{base_url}/{href}"
                
                current_links[href] = text
                
    except Exception as e:
        print(f"Ошибка при проверке УКС Запад: {e}")
        return notifications

    if is_first_run:
        print("УКС Запад: Первый запуск. Сохраняем ссылки из основного контента...")
        with open(UKS_ZAPAD_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(current_links.keys()), f, indent=4, ensure_ascii=False)
        return notifications

    new_links = {href: text for href, text in current_links.items() if href not in processed_links}
    
    if not new_links:
        print("УКС Запад: Нет новых публикаций.")
        return notifications

    for href, text in new_links.items():
        print(f"УКС Запад: Найдено новое -> {text}")
        msg = (
            f"🏗 <b>Новое обновление на УКС Запад!</b>\n\n"
            f"<b>Текст:</b> {text}\n"
            f"<a href='{href}'>Перейти к публикации</a>"
        )
        notifications.append(msg)
        processed_links.add(href)

    with open(UKS_ZAPAD_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(processed_links), f, indent=4, ensure_ascii=False)
        
    return notifications


def check_mapid():
    """Источник 3: Проверка новых ссылок в основном контенте на сайте МАПИД."""
    notifications = []
    processed_links = set()
    
    if os.path.exists(MAPID_STATE_FILE):
        with open(MAPID_STATE_FILE, 'r', encoding='utf-8') as f:
            try:
                processed_links = set(json.load(f))
            except json.JSONDecodeError:
                pass

    is_first_run = not os.path.exists(MAPID_STATE_FILE)

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(MAPID_URL, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # --- ВАЖНОЕ ОБНОВЛЕНИЕ ---
        for tag in soup(['nav', 'header', 'footer', 'aside']):
            tag.decompose()
            
        for tag in soup.find_all(lambda t: t.has_attr('class') and any(c in ['menu', 'sidebar', 'breadcrumbs'] for c in t['class'])):
            tag.decompose()
        # -------------------------
        
        current_links = {}
        for link in soup.find_all('a', href=True):
            href = link['href']
            text = link.get_text(strip=True) or "Без названия"
            
            if len(text) > 1 and not href.startswith(('mailto:', '#', 'javascript:', 'tel:')):
                if not href.startswith('http'):
                    base_url = "https://mapid.by"
                    href = base_url + href if href.startswith('/') else f"{base_url}/{href}"
                
                current_links[href] = text
                
    except Exception as e:
        print(f"Ошибка при проверке МАПИД: {e}")
        return notifications

    if is_first_run:
        print("МАПИД: Первый запуск. Сохраняем ссылки из основного контента...")
        with open(MAPID_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(current_links.keys()), f, indent=4, ensure_ascii=False)
        return notifications

    new_links = {href: text for href, text in current_links.items() if href not in processed_links}
    
    if not new_links:
        print("МАПИД: Нет новых публикаций.")
        return notifications

    for href, text in new_links.items():
        print(f"МАПИД: Найдено новое -> {text}")
        msg = (
            f"🏢 <b>Новое обновление на МАПИД!</b>\n\n"
            f"<b>Текст:</b> {text}\n"
            f"<a href='{href}'>Перейти к публикации</a>"
        )
        notifications.append(msg)
        processed_links.add(href)

    with open(MAPID_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(processed_links), f, indent=4, ensure_ascii=False)
        
    return notifications

def check_minskstroy():
    """Источник 4: Проверка новых ссылок в основном контенте на сайте Минскстрой."""
    notifications = []
    processed_links = set()
    
    if os.path.exists(MINSKSTROY_STATE_FILE):
        with open(MINSKSTROY_STATE_FILE, 'r', encoding='utf-8') as f:
            try:
                processed_links = set(json.load(f))
            except json.JSONDecodeError:
                pass

    is_first_run = not os.path.exists(MINSKSTROY_STATE_FILE)

    try:
        session = get_robust_session()
        response = session.get(MINSKSTROY_URL, timeout=(10, 30))
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Вырезаем шапку, навигацию, подвал и боковые панели
        for tag in soup(['nav', 'header', 'footer', 'aside']):
            tag.decompose()
            
        # Убираем пагинацию (номера страниц) и меню, чтобы не было ложных срабатываний
        for tag in soup.find_all(lambda t: t.has_attr('class') and any(c in ['menu', 'sidebar', 'breadcrumbs', 'pagination'] for c in t['class'])):
            tag.decompose()
        
        current_links = {}
        for link in soup.find_all('a', href=True):
            href = link['href']
            text = link.get_text(strip=True) or "Без названия"
            
            if len(text) > 1 and not href.startswith(('mailto:', '#', 'javascript:', 'tel:')):
                if not href.startswith('http'):
                    base_url = "https://minskstroy.by"
                    href = base_url + href if href.startswith('/') else f"{base_url}/{href}"
                
                current_links[href] = text
                
    except Exception as e:
        print(f"Ошибка при проверке Минскстрой: {e}")
        return notifications

    if is_first_run:
        print("Минскстрой: Первый запуск. Сохраняем ссылки из основного контента...")
        with open(MINSKSTROY_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(current_links.keys()), f, indent=4, ensure_ascii=False)
        return notifications

    new_links = {href: text for href, text in current_links.items() if href not in processed_links}
    
    if not new_links:
        print("Минскстрой: Нет новых публикаций.")
        return notifications

    for href, text in new_links.items():
        print(f"Минскстрой: Найдено новое -> {text}")
        msg = (
            f"🏗 <b>Новое объявление на Минскстрой!</b>\n\n"
            f"<b>Текст:</b> {text}\n"
            f"<a href='{href}'>Перейти к публикации</a>"
        )
        notifications.append(msg)
        processed_links.add(href)

    with open(MINSKSTROY_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(processed_links), f, indent=4, ensure_ascii=False)
        
    return notifications


def check_minsk_courier():
    """Источник 5: Проверка Минского курьера через архив Белкиоска."""
    notifications = []
    
    BELKIOSK_LOGIN = os.getenv("BELKIOSK_LOGIN")
    BELKIOSK_PASSWORD = os.getenv("BELKIOSK_PASSWORD")
    
    if not BELKIOSK_LOGIN or not BELKIOSK_PASSWORD:
        print("Белкиоск: Учетные данные не заданы, пропускаем.")
        return notifications

    STATE_FILE = "belkiosk_state.json"
    processed_pdfs = set()
    
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            try:
                processed_pdfs = set(json.load(f))
            except json.JSONDecodeError:
                pass

    try:
        session = get_robust_session()
        
        # 1. Загружаем страницу входа, чтобы получить CSRF токен и структуру формы
        login_url = "https://belkiosk.by/user/login"
        resp = session.get(login_url, timeout=(10, 30))
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Динамически ищем форму с вводом пароля и собираем payload
        payload = {}
        for form in soup.find_all('form'):
            if form.find('input', type='password'):
                for input_tag in form.find_all('input'):
                    name = input_tag.get('name')
                    if not name: 
                        continue
                        
                    type_attr = input_tag.get('type', '').lower()
                    if type_attr == 'password':
                        payload[name] = BELKIOSK_PASSWORD
                    elif type_attr in ['text', 'email', 'tel'] and 'username' in name.lower():
                        payload[name] = BELKIOSK_LOGIN
                    elif type_attr in ['text', 'email', 'tel'] and not any(v == BELKIOSK_LOGIN for v in payload.values()):
                        payload[name] = BELKIOSK_LOGIN
                    else:
                        # Захватываем скрытые поля, такие как YII_CSRF_TOKEN
                        payload[name] = input_tag.get('value', '')
                break
        
        # 2. Отправляем запрос на авторизацию
        session.post(login_url, data=payload, timeout=(10, 30))
        
        # 3. Переходим в личный кабинет (архив)
        archive_url = "https://belkiosk.by/archive"
        archive_resp = session.get(archive_url, timeout=(10, 30))
        archive_soup = BeautifulSoup(archive_resp.text, 'html.parser')
        
        # Проверяем успешность авторизации по наличию ссылки "Выйти"
        if not archive_soup.find('a', href='/user/logout'):
            print("Белкиоск: Ошибка авторизации. Проверьте логин и пароль.")
            return notifications
            
        # 4. Ищем выпуски Минского курьера
        current_links = {}
        for row in archive_soup.find_all('tr'):
            th = row.find('th', class_='label')
            if th and 'Минский курьер' in th.text:
                a_tag = row.find('a')
                if a_tag and a_tag.get('href') and '/downloads/' in a_tag.get('href'):
                    href = "https://belkiosk.by" + a_tag.get('href')
                    current_links[href] = th.text.strip()
                    
        # Фильтруем только новые выпуски
        new_pdfs = {href: text for href, text in current_links.items() if href not in processed_pdfs}
        
        if not new_pdfs:
            print("Белкиоск (Минский курьер): Нет новых выпусков для проверки.")
            return notifications
            
        # 5. Скачиваем и проверяем новые PDF
        for pdf_url, title in new_pdfs.items():
            print(f"Белкиоск: Проверка {title}...")
            
            # Скачиваем PDF используя АВТОРИЗОВАННУЮ сессию
            res = session.get(pdf_url, timeout=(10, 60))
            res.raise_for_status()
            
            pdf_file = BytesIO(res.content)
            reader = PdfReader(pdf_file)
            
            found = False
            for page_num, page in enumerate(reader.pages, 1):
                text = page.extract_text()
                if text and KEYWORD_REGEX.search(text):
                    msg = (
                        f"🗞 <b>Найдена проектная декларация!</b>\n\n"
                        f"<b>Источник:</b> {title} (Минский курьер)\n"
                        f"<b>Страница:</b> {page_num}\n"
                        f"<a href='https://belkiosk.by/archive'>Перейти в архив Белкиоска</a>"
                    )
                    notifications.append(msg)
                    found = True
                    break # Достаточно одного совпадения в файле
            
            if not found:
                print(f"Белкиоск: В выпуске '{title}' ключевых слов не найдено.")
                
            processed_pdfs.add(pdf_url)
            
        # 6. Сохраняем состояние
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(processed_pdfs), f, indent=4, ensure_ascii=False)
            
    except Exception as e:
        print(f"Ошибка при обработке Белкиоск: {e}")
        
    return notifications


def check_minsk_gov_rental():
    """Источник 6: Проверка новых предложений арендного жилья (СТРОГО ТОЛЬКО КВАРТИРЫ)."""
    notifications = []
    STATE_FILE = MINSK_GOV_RENTAL_STATE_FILE
    
    processed_items = set()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            try:
                processed_items = set(json.load(f))
            except json.JSONDecodeError:
                pass

    is_first_run = not os.path.exists(STATE_FILE)

    # Стоп-слова (адреса исполкомов и инструкции), чтобы не спутать их с квартирами
    STOP_WORDS = [
        'одно окно', 'заявлен', 'учет', 'кодекс', 'прием',
        'кальварийская', 'жилуновича', 'маяковского', 'нёманская', 'неманская', 'мельникайте'
    ]

    try:
        session = get_robust_session()
        response = session.get(MINSK_GOV_RENTAL_URL, timeout=(10, 30))
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        # Удаляем лишний мусор со страницы
        for tag in soup(['nav', 'header', 'footer', 'aside', 'script', 'style']):
            tag.decompose()
            
        current_items = {}

        # Проходимся по всем строкам таблиц и абзацам на странице
        for row in soup.find_all(['tr', 'p', 'li']):
            text = row.get_text(" ", strip=True)
            text_lower = text.lower()
            
            # ЖЕСТКИЙ ФИЛЬТР: Ищем корни слов, чтобы ловить любые падежи (дом, доме, дома)
            has_street = any(x in text_lower for x in ['ул.', 'улиц', 'пр-т', 'просп', 'пер.', 'тракт'])
            has_house = any(x in text_lower for x in ['д.', 'дом'])
            has_apt = any(x in text_lower for x in ['кв.', 'квартир'])
            
            if has_street and has_house and has_apt and any(c.isdigit() for c in text):
                
                # Отсеиваем инструкции, если они случайно прошли фильтр
                if any(sw in text_lower for sw in STOP_WORDS):
                    continue
                    
                # Очищаем от двойных пробелов и переносов строк
                clean_text = re.sub(r'\s+', ' ', text).strip()
                
                # Квартира должна быть разумной длины (не захватывать полстраницы)
                if 15 < len(clean_text) < 400:
                    
                    # Ищем название района, поднимаясь вверх по странице
                    district = "Мингорисполком (Арендное жилье)"
                    for prev in row.find_all_previous(['h2', 'h3', 'h4', 'h5', 'b', 'strong', 'td', 'div']):
                        prev_text = prev.get_text(strip=True)
                        if 'администрация' in prev_text.lower() and len(prev_text) < 100:
                            district = prev_text
                            break
                    
                    unique_id = f"{district}_{clean_text[:100]}"
                    current_items[unique_id] = {
                        "district": district,
                        "text": clean_text,
                        "url": MINSK_GOV_RENTAL_URL
                    }

    except Exception as e:
        print(f"Ошибка при проверке Арендного жилья: {e}")
        return notifications

    if is_first_run:
        print("Арендное жилье: Первый запуск. Сохраняем ТОЛЬКО квартиры как базу...")
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(current_items.keys()), f, indent=4, ensure_ascii=False)
        return notifications

    new_keys = [k for k in current_items.keys() if k not in processed_items]

    if not new_keys:
        print("Арендное жилье: Нет новых квартир.")
        return notifications

    for key in new_keys:
        item = current_items[key]
        print(f"Арендное жилье: Найдено новое -> {item['district']}: {item['text']}")
        msg = (
            f"🔑 <b>Новое арендное жилье!</b>\n\n"
            f"<b>Орган:</b> {item['district']}\n"
            f"<b>Квартира:</b> {item['text']}\n"
            f"<a href='{item['url']}'>Смотреть на сайте</a>"
        )
        notifications.append(msg)
        processed_items.add(key)

    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(processed_items), f, indent=4, ensure_ascii=False)

    return notifications

# ==========================================
# ГЛАВНАЯ ФУНКЦИЯ
# ==========================================

def main():
    print("Запуск проверки всех источников...")
    all_notifications = []
    
    # 1. Сбор уведомлений со всех источников
    all_notifications.extend(check_zviazda_pdfs())
    all_notifications.extend(check_uks_zapad())
    all_notifications.extend(check_mapid())
    all_notifications.extend(check_minskstroy())
    all_notifications.extend(check_minsk_courier())
    all_notifications.extend(check_minsk_gov_rental())

    # 2. Отправка уведомлений в Telegram
    if all_notifications:
        print(f"Найдено новых совпадений: {len(all_notifications)}! Отправляем в Telegram...")
        for message in all_notifications:
            send_telegram_message(message)
            # Небольшая пауза, чтобы не упереться в лимиты Telegram API (особенно если уведомлений много)
            time.sleep(1) 
    else:
        print("Ничего нового не найдено. Уведомления не требуются.")
        
    print("Проверка завершена.")

if __name__ == "__main__":
    main()