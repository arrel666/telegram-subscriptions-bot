"""Persistent buyer navigation and four-step request wizard."""
import html
import json
import secrets


def b(text, action):
    return {'text': text, 'callback_data': action}

FIELDS = ('purpose', 'duration', 'country')
OPTIONS = (
    ('Для себя', 'В подарок', 'Помогите выбрать'),
    ('1 месяц', '3 месяца', '6 месяцев', '12 месяцев', 'Помогите со сроком'),
    ('Россия', 'Казахстан', 'Турция', 'Другая страна', 'Не знаю страну аккаунта'),
)
QUESTIONS = ('Для кого подписка?', 'На какой срок?', 'Какая страна указана в аккаунте?')
ALIASES = {'chatgpt':'чатгпт чат гпт', 'spotify':'спотифай споти', 'telegram_premium':'телеграм премиум',
           'youtube':'ютуб ютюб', 'claude':'клод клауд', 'canva':'канва', 'capcut':'капкат капкут',
           'netflix':'нетфликс', 'apple_music':'эпл музыка', 'midjourney':'миджорни', 'icloud':'айклауд'}


class Storefront:
    def clear_selection(self, uid):
        self.s.run('DELETE FROM selections WHERE user_id=?', (uid,))

    def selection(self, uid):
        row = self.s.one('SELECT * FROM selections WHERE user_id=?', (uid,))
        if row and row['created'] >= self.now() - 3600:
            return json.loads(row['data'])
        self.clear_selection(uid)
        return None

    def save_selection(self, uid, d):
        self.s.run('INSERT INTO selections VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
                   'data=excluded.data,created=excluded.created', (uid, json.dumps(d), self.now()))

    def is_favorite(self, uid, pid):
        return bool(self.s.one('SELECT 1 FROM favorites WHERE user_id=? AND product_id=?', (uid, pid)))

    def favorites(self, uid):
        self.clear_selection(uid)
        self.s.run('DELETE FROM drafts WHERE user_id=?', (uid,))
        rows = [[b(p['title'], 'product:'+p['id'])] for p in self.catalog.values() if self.is_favorite(uid, p['id'])]
        self.screen(uid, '<b>Твои избранные подписки</b>\n'+('Выбери сервис.' if rows else
                 'Нажми ♡ в карточке сервиса, чтобы сохранить его здесь.'),
                 rows+[[b('Каталог', 'catalog'), b('Главное меню', 'home')]])

    def search(self, uid, query=''):
        self.reset_flows(uid)
        self.s.run('DELETE FROM drafts WHERE user_id=?', (uid,))
        if not query.strip():
            self.save_selection(uid, {'mode':'search'})
            self.screen(uid, '<b>Найдём твою подписку</b>\nНапиши название, например Spotify или «чатгпт».', [[b('Каталог','catalog')]])
            return
        self.clear_selection(uid)
        words = query.casefold().split()
        products = [p for p in self.catalog.values() if all(w in (p['title']+' '+ALIASES.get(p['id'],'')).casefold()
                    for w in words)] if len(query) <= 100 else []
        self.screen(uid, '<b>Результаты поиска</b>\n'+(f'Найдено: {len(products)}.' if products else
                 'Не нашли. Попробуй другое название или спроси поддержку.'),
                 [[b(p['title'],'product:'+p['id'])] for p in products]+
                 [[b('Искать ещё','search'),b('Каталог','catalog')],[b('Поддержка','support')]])

    def wizard(self, uid, pid):
        self.reset_flows(uid)
        if pid not in self.catalog:
            self.screen(uid,'Сервис сейчас недоступен.',[[b('Каталог','catalog')]]); return
        self.s.run('DELETE FROM drafts WHERE user_id=?',(uid,))
        d={'mode':'wizard','pid':pid,'step':0,'nonce':secrets.token_hex(4)}
        self.save_selection(uid,d); self.wizard_screen(uid,d)

    def wizard_options(self, d, step):
        if step == 1 and d['pid'] == 'telegram_premium':
            return ('3 месяца', '6 месяцев', '12 месяцев', 'Помогите со сроком')
        return OPTIONS[step]

    def wizard_screen(self, uid, d):
        step, nonce = d['step'], d['nonce']
        title = html.escape(self.catalog[d['pid']]['title'])
        if step < 3:
            text=f'<b>{title}</b>\nШаг {step+1} из 4 · {QUESTIONS[step]}\n\n'
            text += ('Страна аккаунта может отличаться от места проживания.' if step==2 else
                     'Это пожелание. Доступный тариф подтвердим до оплаты.')
            rows=[[b(value,f'choose:{nonce}:{step}:{i}')] for i,value in enumerate(self.wizard_options(d, step))]
        else:
            text=f'<b>Проверь заявку · {title}</b>\nШаг 4 из 4\n\n'
            text+='\n'.join(html.escape(d[k]) for k in FIELDS)
            text+='\n\nЦена: после проверки доступности.\nПришлём тариф, итоговую сумму и срок подключения.\nСейчас оплаты нет. Автосписаний нет.'
            rows=[[b('Всё верно · Узнать цену',f'submit:{nonce}')]]
        if step: rows.append([b('← Назад',f'back:{nonce}')])
        rows.append([b('Отменить выбор','catalog')])
        self.screen(uid,text,rows)

    def storefront_callback(self, uid, data):
        if data=='search': self.search(uid)
        elif data=='favorites': self.favorites(uid)
        elif data=='how':
            self.screen(uid,'<b>От выбора до подключения</b>\n\n1. Выбери сервис и оставь пожелания.\n'
                     '2. Получи точную цену, тариф и срок выдачи.\n3. Проверь условия и оплати в Telegram Stars.\n'
                     '4. Получи инструкцию в этом чате.\n\nКаждая покупка разовая. Пароли и коды входа не нужны.',
                     [[b('Выбрать подписку','catalog')],[b('Поддержка','support'),b('Главное меню','home')]])
        elif data.startswith('favorite:'):
            pid=data.split(':',1)[1]
            if pid in self.catalog:
                if self.is_favorite(uid,pid): self.s.run('DELETE FROM favorites WHERE user_id=? AND product_id=?',(uid,pid))
                else: self.s.run('INSERT OR IGNORE INTO favorites VALUES (?,?)',(uid,pid))
                self.product(uid,pid)
        elif data.startswith('wizard:'): self.wizard(uid,data.split(':',1)[1])
        elif data.startswith(('choose:','back:','submit:')):
            parts=data.split(':'); d=self.selection(uid)
            if not d or d.get('mode')!='wizard' or parts[1]!=d['nonce'] or d['pid'] not in self.catalog:
                self.screen(uid,'Этот выбор уже закрыт. Открой подписку заново.',[[b('Каталог','catalog')]]); return True
            if parts[0]=='choose':
                try:
                    if len(parts)!=4 or int(parts[2])!=d['step'] or d['step']>=3: raise ValueError()
                    index=int(parts[3])
                    if not 0<=index<len(self.wizard_options(d, d['step'])): raise ValueError()
                    d[FIELDS[d['step']]]=self.wizard_options(d, d['step'])[index]
                except (ValueError,IndexError): self.wizard_screen(uid,d); return True
                if d['step']==2 and index==3:
                    d['mode']='country'; self.save_selection(uid,d)
                    self.screen(uid,'<b>Укажи страну аккаунта</b>\nНапиши название страны одним сообщением.',
                             [[b('Начать заново','wizard:'+d['pid']),b('Отмена','catalog')]]); return True
                d['step']+=1
            elif parts[0]=='back': d['step']=max(0,d['step']-1)
            elif d['step']==3:
                details='; '.join(d[k] for k in FIELDS)
                self.clear_selection(uid)
                self.s.run('INSERT INTO drafts VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
                           'product_id=excluded.product_id,created=excluded.created',(uid,d['pid'],self.now()))
                self.message(uid,details); return True
            d['nonce']=secrets.token_hex(4); self.save_selection(uid,d); self.wizard_screen(uid,d)
        elif data.startswith(('helporder:','cancelorder:','confirmcancel:')):
            action,oid=data.split(':',1); o=self.own_order(uid,oid)
            if action=='helporder':
                self.screen(uid,f'<b>Помощь с заказом</b>\n{html.escape(o["title"])}\n'
                         f'Скопируй номер: <code>{html.escape(oid)}</code> и отправь его поддержке.',
                         [[b('Задать вопрос в боте','ticketnew:'+oid)],
                          [{'text':'Написать в поддержку','url':'https://t.me/'+self.cfg.support}],
                          [b('Вернуться к заказу','order:'+oid)]])
            elif uid==o['user_id'] and o['status'] in ('requested','quoted'):
                if action=='cancelorder':
                    self.screen(uid,'<b>Отменить заказ?</b>\nМожно вернуться к выбору позже.',
                             [[b('Да, отменить','confirmcancel:'+oid)],[b('Оставить заказ','order:'+oid)]])
                else:
                    self.s.run("UPDATE orders SET status='cancelled' WHERE id=?",(oid,)); self.show_order(uid,oid)
            else: self.show_order(uid,oid)
        else: return False
        return True

    def storefront_message(self, uid, text):
        d=self.selection(uid)
        if not d: return False
        if d['mode']=='search': self.search(uid,text)
        elif d['pid'] not in self.catalog: self.categories(uid)
        elif d['mode']=='country':
            if not 2<=len(text.strip())<=60 or not any(c.isalpha() for c in text):
                self.screen(uid,'Напиши название страны: от 2 до 60 символов.'); return True
            d.update(country=text.strip(),mode='wizard',step=3,nonce=secrets.token_hex(4))
            self.save_selection(uid,d); self.wizard_screen(uid,d)
        else: self.wizard_screen(uid,d)
        return True

    def order_next_step(self, o):
        state=o['status']
        if state=='requested': return '◉ Заявка → Цена → Оплата → Выдача\nПредложение придёт в этот чат. Платить пока не нужно.'
        if state=='quoted':
            if o['expires']<self.now(): return 'Предложение истекло. Запроси актуальную цену в поддержке.'
            from datetime import datetime, timezone
            end=datetime.fromtimestamp(o['expires'],timezone.utc).strftime('%d.%m %H:%M UTC')
            return f'✓ Заявка · ◉ Оплата · ○ Выдача\nИтого: {o["price"]} ⭐. Без автосписаний.\nЦена действует до {end}.'
        return {'checkout':'Telegram подтверждает оплату. Обнови статус через минуту.',
                'paid':'✓ Оплачено · ◉ Подключение\nИнструкция придёт в срок из предложения.',
                'delivery_queued':'Инструкция подготовлена и ожидает отправки.',
                'delivery_sending':'Инструкция отправляется в этот чат.',
                'delivered':'✓ Заказ выдан\nСохрани инструкцию. Если подключение не получилось — нажми «Помощь с заказом».',
                'refund_pending':'Возврат в обработке. Здесь появится подтверждение Telegram.',
                'refunded':'Возврат подтверждён. Проверь историю Telegram Stars.',
                'cancelled':'Заказ отменён. Можно создать новую заявку.',
                'rejected':'Предложение недоступно. Поддержка поможет уточнить альтернативу.'}.get(state,'')
