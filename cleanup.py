import re

with open('funpay.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = re.sub(r'                    new_fb.*?Обнаружен системный отзыв.*?group\(1\)\}\"\)\)', '', text, flags=re.DOTALL)
text = text.replace('                    # Проверка на системное сообщение об отзыве', '')

with open('funpay.py', 'w', encoding='utf-8') as f:
    f.write(text)

print('Done funpay.py cleanup')
