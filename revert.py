import re

with open('main.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('import logger\n', '')
text = re.sub(r'logger\.(info|success|warning|error|bump|notify)\((.*?)\)', r'print(\2)', text)
text = text.replace('logger.banner(client.account.username)', 'print_banner()')
if 'def print_banner()' not in text:
    banner = '''def print_banner():
    banner_text = \"\"\"
  ___               _              ____       _   
 | __|  _ _ _  _ __| |___ _  _    | _ ) ___ _| |_ 
 | _| || | \\' \\/ _ / -_) || |   | _ \\/ _ \\  _|
 |_| \\_,_|_||_\\__,_\\___|\\_, |   |___/\\___/\\__|
                        |__/                  
\"\"\"
    print(banner_text)
'''
    text = text.replace('async def _fetch_and_send_review', banner + '\nasync def _fetch_and_send_review')

# Revert 20-second logic
text = text.replace('_review_check_immediate', '_review_check')
if 'await asyncio.sleep(20)' not in text:
    text = text.replace('async def _fetch_and_send_review(bot: Bot, client: FunPayClient, order_id: str, buyer: str):', 'async def _fetch_and_send_review(bot: Bot, client: FunPayClient, order_id: str, buyer: str):\n    await asyncio.sleep(20)')

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(text)

with open('funpay.py', 'r', encoding='utf-8') as f:
    ftext = f.read()

ftext = ftext.replace('import logger\n', '')
ftext = re.sub(r'logger\.(info|success|warning|error|bump|notify|debug)\((.*?)\)', r'print(\2)', ftext)

# Revert runner/ patch logic
if 'if api_method == \"runner/\":' in ftext:
    ftext = re.sub(r'                if api_method == \"runner/\":.*?else:\n                    # Все ОСТАЛЬНЫЕ.*?with self\._account_lock:\n                        return original_method\(request_method, api_method, headers, payload, \*args, \*\*kwargs\)', r'''                with self._account_lock:
                    try:
                        return original_method(request_method, api_method, headers, payload, *args, **kwargs)
                    except Exception as e:
                        if api_method == "runner/":
                            print(f"[NOTIFY][RUNNER DEBUG] Реальная ошибка запроса runner/: {type(e).__name__}: {e}")
                            resp = getattr(e, "response", None)
                            if resp is not None:
                                try:
                                    print(f"[NOTIFY][RUNNER DEBUG] Тело ответа сервера: {resp.text[:1000]}")
                                except Exception:
                                    pass
                            for attr in ("response_text", "text", "body", "message", "msg"):
                                val = getattr(e, attr, None)
                                if val:
                                    print(f"[NOTIFY][RUNNER DEBUG] e.{attr} = {str(val)[:1000]}")
                        raise''', ftext, flags=re.DOTALL)

with open('funpay.py', 'w', encoding='utf-8') as f:
    f.write(ftext)

print('Reverted successfully')
