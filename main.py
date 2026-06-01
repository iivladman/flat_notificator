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

# ==========================================
# БАЗОВЫЕ ФУНКЦИИ
# ==========================================

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
        response = requests.get(ZVIAZDA_URL)
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
            res = requests.get(pdf_url)
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
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(MINSKSTROY_URL, headers=headers)
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