import logging
import telebot
import os
import tempfile
import chardet
import re
from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from datetime import datetime

# === КОНФИГУРАЦИЯ ===
ADMIN_TOKEN = 'Token'
ADMIN_CHAT_ID = ID
DB_URL = "postgresql://user:password@localhost/dbname"

# === ИНИЦИАЛИЗАЦИЯ ===
bot = telebot.TeleBot(ADMIN_TOKEN)
engine = create_engine(DB_URL)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("AdminBot")

# === ЗАГЛУШКИ ДЛЯ ТЕСТИРОВАНИЯ ===
USERS = {
    "@geb_mi": {"user_id": 1, "user_type": "teacher", "full_name": "Михаил Геб", "chat_id": 1749719980},
    "@Herman_Gebel": {"user_id": 2, "user_type": "student", "full_name": "Маша", "group_id": 1, "chat_id": 11111111},
    "@amaro_nonino": {"user_id": 3, "user_type": "student", "full_name": "Саша", "group_id": 1, "chat_id": 997855184},
}

GROUPS = {
    1: {"group_id": 1, "group_code": "Группа 1", "course": 1},
    2: {"group_id": 2, "group_code": "Группа 2", "course": 1},
}

LESSONS = [
    {"lesson_id": 1, "subject_id": 1, "teacher_id": 1, "lesson_date": "2025-06-23", "lesson_time": "10:00-11:30",
     "groups": ["Группа 1"]},
    {"lesson_id": 2, "subject_id": 2, "teacher_id": 1, "lesson_date": "2025-06-24", "lesson_time": "12:00-13:30",
     "groups": ["Группа 2"]},
]

# Глобальная переменная для хранения года
CURRENT_YEAR = 2025


# === ПРОВЕРКА АДМИНСКИХ ПРАВ ===
def is_admin(message):
    try:
        return message.from_user.id == ADMIN_CHAT_ID
    except Exception as e:
        logger.error(f"Ошибка проверки админских прав: {e}")
        return False


# === ФУНКЦИЯ ТРАНСЛИТЕРАЦИИ ===
def simple_translit(text):
    translit_dict = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e', 'ж': 'zh',
        'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
        'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts',
        'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu',
        'я': 'ya',
        ' ': '_'
    }
    result = []
    for char in text.lower():
        if char in translit_dict:
            result.append(translit_dict[char])
        elif char in 'abcdefghijklmnopqrstuvwxyz_0123456789':
            result.append(char)
    return ''.join(result)


# === ГЕНЕРАЦИЯ USERNAME ===
def generate_username(full_name):
    full_name = ' '.join(full_name.split())
    parts = full_name.split()
    if len(parts) == 0:
        return "student"

    if len(parts) >= 2:
        base = parts[0] + '_' + parts[1]
    else:
        base = parts[0]

    base_translit = simple_translit(base)
    if not base_translit:
        return "student"

    return base_translit.lower()


# === ИЗВЛЕЧЕНИЕ НАЗВАНИЯ ГРУППЫ ИЗ HTML ===
def extract_group_name(soup):
    # Попробуем найти название группы в заголовках
    headers = soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    for header in headers:
        text = header.get_text().strip()
        if re.search(r'[Гг]руппа\s*[№]?\s*[\w-]+', text):
            return text

    # Поиск в таблице
    table = soup.find('table')
    if table:
        # Проверим заголовки столбцов
        header_row = table.find('tr')
        if header_row:
            headers = [th.get_text().strip().lower() for th in header_row.find_all(['th', 'td'])]
            if 'группа' in headers:
                group_col_index = headers.index('группа')

                # Найдем первую строку с данными
                for row in table.find_all('tr')[1:]:
                    cols = row.find_all('td')
                    if len(cols) > group_col_index:
                        group_name = cols[group_col_index].get_text().strip()
                        if group_name:
                            return group_name

        # Если не нашли в заголовках, поищем в ячейках
        for row in table.find_all('tr'):
            for cell in row.find_all('td'):
                text = cell.get_text().strip()
                match = re.search(r'[Гг]руппа\s*[№]?\s*([\w-]+)', text)
                if match:
                    return f"Группа {match.group(1)}"

    # Попробуем найти в тексте документа
    body_text = soup.get_text()
    match = re.search(r'[Гг]руппа\s*[№]?\s*([\w-]+)', body_text)
    if match:
        return f"Группа {match.group(1)}"

    return "Неизвестная группа"


# === ОБРАБОТКА HTML-ФАЙЛА ГРУППЫ ===
def process_group_file(file_path):
    # Читаем файл в бинарном режиме
    with open(file_path, 'rb') as f:
        raw_data = f.read()

    # Автоопределение кодировки
    encoding_detected = chardet.detect(raw_data)
    encoding = encoding_detected['encoding'] or 'utf-8'

    try:
        # Декодируем с определенной кодировкой
        content = raw_data.decode(encoding)
    except UnicodeDecodeError:
        # Пробуем альтернативные кодировки
        for alt_encoding in ['cp1251', 'iso-8859-5', 'koi8-r', 'windows-1251']:
            try:
                content = raw_data.decode(alt_encoding)
                encoding = alt_encoding
                break
            except UnicodeDecodeError:
                continue
        else:
            # Если все варианты не подошли, используем замену ошибок
            content = raw_data.decode('utf-8', errors='replace')

    # Парсим HTML
    soup = BeautifulSoup(content, 'html.parser')

    # Извлекаем название группы
    group_name = extract_group_name(soup)

    # Определяем group_id (создаем новую группу если нужно)
    group_id = None
    new_group_added = False
    for gid, group in GROUPS.items():
        if group['group_code'] == group_name:
            group_id = gid
            break

    if group_id is None:
        group_id = max(GROUPS.keys()) + 1 if GROUPS else 1
        GROUPS[group_id] = {
            "group_id": group_id,
            "group_code": group_name,
            "course": 1
        }
        new_group_added = True

    # Обрабатываем студентов
    table = soup.find('table')
    if not table:
        raise ValueError("HTML файл не содержит таблицы")

    rows = table.find_all('tr')[1:]  # Пропускаем заголовок
    updated_students = 0
    new_students = 0

    for row in rows:
        cols = row.find_all('td')
        if len(cols) < 2:  # Нужен минимум 1 столбец с ФИО
            continue

        full_name = cols[1].text.strip()

        # Поиск студента
        student_exists = False
        for username, data in USERS.items():
            if data.get('full_name') == full_name and data['user_type'] == 'student':
                # Обновляем группу существующего студента
                data['group_id'] = group_id
                updated_students += 1
                student_exists = True
                break

        if not student_exists:
            # Создаем нового студента
            base_username = generate_username(full_name)
            username = base_username
            counter = 1

            # Убеждаемся в уникальности username
            while username in USERS:
                username = f"{base_username}{counter}"
                counter += 1

            new_user_id = max(user['user_id'] for user in USERS.values()) + 1
            USERS[username] = {
                "user_id": new_user_id,
                "user_type": "student",
                "full_name": full_name,
                "group_id": group_id,
                "chat_id": None
            }
            new_students += 1

    return group_name, updated_students, new_students, new_group_added, encoding


# === ОБРАБОТКА РАСПИСАНИЯ ПРЕПОДАВАТЕЛЕЙ ===
def process_teacher_schedule(file_path):
    # Читаем файл в бинарном режиме
    with open(file_path, 'rb') as f:
        raw_data = f.read()

    # Автоопределение кодировки
    encoding_detected = chardet.detect(raw_data)
    encoding = encoding_detected['encoding'] or 'windows-1251'

    try:
        # Декодируем с определенной кодировкой
        content = raw_data.decode(encoding)
    except UnicodeDecodeError:
        # Пробуем альтернативные кодировки
        for alt_encoding in ['cp1251', 'iso-8859-5', 'koi8-r', 'windows-1251']:
            try:
                content = raw_data.decode(alt_encoding)
                encoding = alt_encoding
                break
            except UnicodeDecodeError:
                continue
        else:
            content = raw_data.decode('utf-8', errors='replace')

    # Парсим HTML
    soup = BeautifulSoup(content, 'html.parser')

    # Словарь для хранения расписания: {преподаватель: [пары]}
    schedule = {}

    # Извлекаем информацию о неделе
    week_header = soup.find('h4')
    week_info = week_header.text.strip() if week_header else "Неизвестная неделя"

    # Сопоставление дней недели с датами
    days_mapping = {
        "ПН": "09.06.2025",
        "ВТ": "10.06.2025",
        "СР": "11.06.2025",
        "ЧТ": "12.06.2025",
        "ПТ": "13.06.2025",
        "СБ": "14.06.2025"
    }

    # Сопоставление временных слотов с интервалами
    time_slots = {
        "8:00": "8:00-9:30",
        "9:45": "9:45-11:15",
        "11:30": "11:30-13:00",
        "13:30": "13:30-15:00",
        "15:15": "15:15-16:45",
        "17:00": "17:00-18:30",
        "18:40": "18:40-20:10",
        "20:25": "20:25-21:55"
    }

    # Находим таблицу с расписанием
    table = soup.find('table', class_='slimtab_nice')
    if not table:
        raise ValueError("Таблица расписания не найдена")

    # Обрабатываем строки таблицы
    rows = table.find_all('tr')[1:]  # Пропускаем заголовок

    current_teacher = None
    teacher_rows = []
    rowspan_count = 0

    for row in rows:
        # Проверяем, является ли строка началом нового преподавателя
        first_cell = row.find('td')
        if first_cell and 'rowspan' in first_cell.attrs:
            # Если у нас есть текущий преподаватель, обрабатываем его
            if current_teacher:
                process_teacher_block(current_teacher, teacher_rows, schedule, days_mapping, time_slots)
                teacher_rows = []

            # Начинаем нового преподавателя
            rowspan_count = int(first_cell['rowspan'])
            teacher_name = first_cell.find('b').text.strip() if first_cell.find('b') else first_cell.text.strip()
            current_teacher = teacher_name
            teacher_rows.append(row)
        elif current_teacher and len(teacher_rows) < rowspan_count:
            # Продолжаем текущего преподавателя
            teacher_rows.append(row)

    # Обрабатываем последнего преподавателя
    if current_teacher:
        process_teacher_block(current_teacher, teacher_rows, schedule, days_mapping, time_slots)

    return schedule, encoding, week_info


def process_teacher_block(teacher_name, rows, schedule, days_mapping, time_slots):
    schedule[teacher_name] = []

    # Индексы столбцов дней недели
    day_columns = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ"]

    for row in rows:
        cells = row.find_all('td')

        # Определяем позицию времени
        time_cell = None
        if len(cells) > 1 and cells[1].get('align') == 'center':
            time_cell = cells[1]
        elif len(cells) > 0 and cells[0].get('align') == 'center':
            time_cell = cells[0]

        if not time_cell:
            continue

        time_text = time_cell.text.strip()
        if time_text not in time_slots:
            continue

        time_interval = time_slots[time_text]

        # Обрабатываем ячейки с занятиями
        for i, day in enumerate(day_columns):
            # Определяем позицию ячейки дня
            day_cell_index = 2 + i if time_cell == cells[1] and len(cells) > 2 + i else 1 + i
            if day_cell_index >= len(cells):
                continue

            day_cell = cells[day_cell_index]
            lesson_div = day_cell.find('div')

            if lesson_div:
                date_str = days_mapping[day]
                try:
                    date_obj = datetime.strptime(date_str, "%d.%m.%Y")
                    date_formatted = date_obj.strftime("%Y-%m-%d")
                except:
                    continue

                # Извлекаем группы
                groups = []
                group_div = day_cell.find('div', text=re.compile(r'Группа\(ы\)'))
                if group_div:
                    next_div = group_div.find_next_sibling('div')
                    if next_div:
                        group_links = next_div.find_all('a')
                        for link in group_links:
                            group_name = link.text.strip()
                            groups.append(group_name)

                schedule[teacher_name].append({
                    "date": date_formatted,
                    "time": time_interval,
                    "groups": groups
                })


# === ОБНОВЛЕНИЕ РАСПИСАНИЯ В БАЗЕ ===
def update_lessons_in_db(schedule):
    updated_lessons = 0
    new_teachers = 0
    new_lessons = 0

    for teacher_name, lessons in schedule.items():
        # Поиск преподавателя в базе
        teacher_id = None
        teacher_exists = False

        # Поиск в существующих пользователях
        for user_data in USERS.values():
            if user_data.get('full_name') == teacher_name and user_data['user_type'] == 'teacher':
                teacher_id = user_data['user_id']
                teacher_exists = True
                break

        # Если преподаватель не найден, создаем нового
        if not teacher_exists:
            base_username = generate_username(teacher_name)
            username = base_username
            counter = 1

            # Убеждаемся в уникальности username
            while username in USERS:
                username = f"{base_username}{counter}"
                counter += 1

            new_user_id = max(user['user_id'] for user in USERS.values()) + 1
            USERS[username] = {
                "user_id": new_user_id,
                "user_type": "teacher",
                "full_name": teacher_name,
                "chat_id": None
            }
            teacher_id = new_user_id
            new_teachers += 1

        # Добавляем занятия
        for lesson in lessons:
            # Проверяем, не существует ли уже такого занятия
            lesson_exists = False
            for existing_lesson in LESSONS:
                if (existing_lesson['teacher_id'] == teacher_id and
                        existing_lesson['lesson_date'] == lesson['date'] and
                        existing_lesson['lesson_time'] == lesson['time']):
                    # Обновляем группы, если они изменились
                    if set(existing_lesson['groups']) != set(lesson['groups']):
                        existing_lesson['groups'] = lesson['groups']
                        updated_lessons += 1
                    lesson_exists = True
                    break

            if not lesson_exists:
                new_lesson_id = max(l['lesson_id'] for l in LESSONS) + 1 if LESSONS else 1
                LESSONS.append({
                    "lesson_id": new_lesson_id,
                    "subject_id": 1,  # ID предмета по умолчанию
                    "teacher_id": teacher_id,
                    "lesson_date": lesson['date'],
                    "lesson_time": lesson['time'],
                    "groups": lesson['groups']
                })
                new_lessons += 1

    return new_teachers, new_lessons, updated_lessons


# === ОБРАБОТЧИК ДОКУМЕНТОВ ===
@bot.message_handler(content_types=['document'])
def handle_document(message):
    if not is_admin(message):
        bot.reply_to(message, "⛔ Доступ запрещен")
        return

    filename = message.document.file_name
    temp_file_path = None

    try:
        # Скачиваем файл
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        # Сохраняем временный файл
        with tempfile.NamedTemporaryFile(delete=False, suffix='.html') as temp_file:
            temp_file.write(downloaded_file)
            temp_file_path = temp_file.name

        # Определяем тип файла по названию
        if "преподавателей" in filename.lower():
            # Обработка расписания преподавателей
            schedule, used_encoding, week_info = process_teacher_schedule(temp_file_path)
            new_teachers, new_lessons, updated_lessons = update_lessons_in_db(schedule)

            report = (
                "✅ Расписание преподавателей успешно обработано\n\n"
                f"📅 Неделя: <b>{week_info}</b>\n"
                f"👨‍🏫 Обработано преподавателей: <b>{len(schedule)}</b>\n"
                f"🆕 Новых преподавателей: <b>{new_teachers}</b>\n"
                f"➕ Добавлено занятий: <b>{new_lessons}</b>\n"
                f"🔄 Обновлено занятий: <b>{updated_lessons}</b>\n"
                f"🔤 Использована кодировка: <b>{used_encoding}</b>\n\n"
                f"ℹ️ Данные заглушек обновлены"
            )

        else:
            # Обработка файла группы
            group_name, updated, new_students, new_group, used_encoding = process_group_file(temp_file_path)
            report = (
                "✅ Файл группы успешно обработан\n\n"
                f"🏷 Название группы: <b>{group_name}</b>\n"
                f"🔤 Использована кодировка: <b>{used_encoding}</b>\n"
                f"🆕 Группа добавлена: {'✅' if new_group else '❌'}\n\n"
                f"👥 Студенты:\n"
                f"• Обновлено: <b>{updated}</b>\n"
                f"• Добавлено: <b>{new_students}</b>\n\n"
                f"ℹ️ Данные заглушек обновлены"
            )

        bot.reply_to(message, report, parse_mode='HTML')
        logger.info(f"Обработан документ: {filename}")

    except Exception as e:
        error_msg = f"❌ Ошибка обработки файла: {str(e)}"
        bot.reply_to(message, error_msg)
        logger.exception(f"Ошибка обработки документа {filename}")

    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            os.unlink(temp_file_path)

# === ОБРАБОТЧИКИ КОМАНД ===
@bot.message_handler(commands=['start'])
def start(message):
    if not is_admin(message):
        bot.reply_to(message, "⛔ Доступ запрещен")
        return

    help_text = (
        "✅ Админ-панель готова к работе\n\n"
        "📊 Доступные команды:\n"
        "/report_attendance - отчет по посещаемости\n"
        "/report_feedback - отчет по оценкам студентов\n"
        "/help - показать это сообщение\n\n"
        "📤 Отправьте HTML-файл:\n"
        "- Группы для обновления состава\n"
        "- Расписание преподавателей для добавления занятий"
    )
    bot.reply_to(message, help_text)


@bot.message_handler(commands=['report_attendance'])
def attendance_report(message):
    if not is_admin(message):
        bot.reply_to(message, "⛔ Доступ запрещен")
        return

    try:
        report = "📊 Отчет по посещаемости:\n\n"
        groups_attendance = {
            "Группа 1": {"attended": 15, "total": 20},
            "Группа 2": {"attended": 18, "total": 22},
        }

        for group, data in groups_attendance.items():
            percentage = (data['attended'] / data['total'] * 100) if data['total'] > 0 else 0
            report += (f"<b>{group}</b>:\n"
                       f"• Присутствовало: <b>{data['attended']}/{data['total']}</b>\n"
                       f"• Процент посещаемости: <b>{percentage:.1f}%</b>\n\n")

        bot.reply_to(message, report, parse_mode='HTML')

    except Exception as e:
        error_msg = f"❌ Ошибка генерации отчета: {str(e)}"
        bot.reply_to(message, error_msg)
        logger.exception("Ошибка генерации отчета посещаемости")


@bot.message_handler(commands=['report_feedback'])
def feedback_report(message):
    if not is_admin(message):
        bot.reply_to(message, "⛔ Доступ запрещен")
        return

    try:
        report = "⭐ Отчет по оценкам студентов:\n\n"
        subjects_ratings = {
            "Математика": {"avg_rating": 4.5, "ratings_count": 30},
            "Физика": {"avg_rating": 4.2, "ratings_count": 25},
            "Программирование": {"avg_rating": 4.8, "ratings_count": 28},
        }

        for subject, data in subjects_ratings.items():
            stars = "★" * int(data['avg_rating'])
            report += (f"<b>{subject}</b>:\n"
                       f"• Средняя оценка: <b>{data['avg_rating']:.2f}</b> {stars}\n"
                       f"• Количество оценок: <b>{data['ratings_count']}</b>\n\n")

        bot.reply_to(message, report, parse_mode='HTML')

    except Exception as e:
        error_msg = f"❌ Ошибка генерации отчета: {str(e)}"
        bot.reply_to(message, error_msg)
        logger.exception("Ошибка генерации отчета оценок")


@bot.message_handler(commands=['help'])
def show_help(message):
    help_text = (
        "📋 <b>Доступные команды:</b>\n\n"
        "/start - начать работу с ботом\n"
        "/report_attendance - отчет по посещаемости\n"
        "/report_feedback - отчет по оценкам студентов\n"
        "/help - показать это сообщение\n\n"
        "📤 <b>Загрузка данных:</b>\n"
        "• Отправьте HTML-файл группы для добавления/обновления данных студентов\n"
        "• Отправьте HTML-файл расписания преподавателей для добавления занятий\n\n"
        "ℹ️ Формат расписания преподавателей:\n"
        "   - Каждая таблица = один преподаватель\n"
        "   - Столбцы: Дата (дд.мм), Время (ЧЧ:ММ-ЧЧ:ММ), Предмет"
    )
    bot.reply_to(message, help_text, parse_mode='HTML')


@bot.message_handler(func=lambda message: True)
def handle_unknown(message):
    bot.reply_to(message, "❌ Неизвестная команда. Используйте /help для списка команд")


# === ЗАПУСК БОТА ===
if __name__ == '__main__':
    logger.info("Админ-бот запущен...")
    try:
        bot.infinity_polling()
    except Exception as e:
        logger.exception("Критическая ошибка в работе бота")
