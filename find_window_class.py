# -*- coding: utf-8 -*-
"""
Скрипт для определения класса окна Kitty.
Автоматически находит все окна Kitty и показывает их класс.
"""

import win32gui
import win32process
import psutil
import os


def find_all_windows():
    """Найти все видимые окна и показать их класс"""
    windows = []
    
    def callback(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title:  # Пропускаем окна без заголовка
                    class_name = win32gui.GetClassName(hwnd)
                    
                    # Получаем имя процесса
                    try:
                        _, pid = win32process.GetWindowThreadProcessId(hwnd)
                        process = psutil.Process(pid)
                        process_name = process.name()
                    except:
                        process_name = "???"
                    
                    windows.append({
                        'title': title,
                        'class': class_name,
                        'process': process_name
                    })
        except:
            pass
        return True
    
    win32gui.EnumWindows(callback, None)
    return windows


def find_kitty_process():
    """Найти PID процесса kitty.exe"""
    for proc in psutil.process_iter(['pid', 'name', 'exe']):
        try:
            if proc.info['name'] and 'kitty' in proc.info['name'].lower():
                return proc.info['pid'], proc.info['name']
        except:
            pass
    return None, None


def main():
    print("=" * 70)
    print("Скрипт определения класса окна Kitty")
    print("=" * 70)
    print()
    
    # Ищем процесс Kitty
    pid, process_name = find_kitty_process()
    
    if pid:
        print(f"✅ Процесс Kitty найден:")
        print(f"   PID: {pid}")
        print(f"   Имя: {process_name}")
        print()
    else:
        print("❌ Процесс Kitty НЕ найден!")
        print("   Запусти Kitty и попробуй снова.")
        print()
    
    print("🔍 Поиск окон Kitty...")
    print()
    
    # Ищем все окна
    all_windows = find_all_windows()
    
    # Фильтруем окна Kitty
    kitty_windows = []
    for w in all_windows:
        title_lower = w['title'].lower()
        class_lower = w['class'].lower()
        
        # Ищем по заголовку или классу
        if ('kitty' in title_lower or 
            'kitty' in class_lower or
            'putty' in class_lower):
            kitty_windows.append(w)
    
    if kitty_windows:
        print("=" * 70)
        print(f"Найдено {len(kitty_windows)} окно(а) Kitty:")
        print("=" * 70)
        for i, w in enumerate(kitty_windows, 1):
            print(f"\n{i}. Заголовок: {w['title']}")
            print(f"   Класс:     {w['class']}  <-- ЭТО НУЖНО!")
            print(f"   Процесс:   {w['process']}")
        print()
        print("=" * 70)
    else:
        print("❌ Окна Kitty не найдены!")
        print()
        print("Показываю ВСЕ окна (найди Kitty в списке):")
        print("=" * 70)
        for i, w in enumerate(all_windows[:20], 1):  # Первые 20
            print(f"{i}. {w['title'][:50]}")
            print(f"   Класс: {w['class']}")
            print(f"   Процесс: {w['process']}")
            print()
    
    print("=" * 70)
    print("Скопируй значение 'Класс' и напиши мне!")
    print("=" * 70)
    print()
    input("Нажми Enter для выхода...")


if __name__ == '__main__':
    main()
