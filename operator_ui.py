"""Button-first administration and in-bot support. Uses existing money workflows."""
import html
import json
import secrets
import uuid
from storefront import b


def e(value):
    return html.escape(str(value))


def fail(message):
    from shop import UserError
    raise UserError(message)


class OperatorUI:
    def operator_dashboard(self, uid):
        self.admin(uid)
        self.reset_flows(uid)
        counts={r[0]:r[1] for r in self.s.all('SELECT status,count(*) FROM orders GROUP BY status')}
        pending=counts.get('requested',0)
        paid=counts.get('paid',0)
        tickets=self.s.one("SELECT count(*) FROM tickets WHERE status='open'")[0]
        revenue=self.s.one("SELECT coalesce(sum(amount),0) FROM payments WHERE is_primary=1 AND status='accepted'")[0]
        self.screen(uid, '<b>Твой магазин</b>\n\n'
                    f'Новые заявки: {pending}\nК выдаче: {paid}\nОткрытые обращения: {tickets}\n'
                    f'Подтверждённые оплаты без возвратов: {revenue} ⭐\n\n'
                    f'Приём оплаты: {"включён" if self.cfg.payments_enabled else "выключен"}.\n'
                    'Сумма оплат не равна прибыли или доступному балансу.',
                    [[b(f'Новые заявки · {pending}','op:filter:requested')],
                     [b(f'Выдать оплаченные · {paid}','op:filter:paid')],
                     [b('Все открытые заказы','queue:0'),b('Обращения','op:tickets')],
                     [b('Статистика','op:stats'),b('Проблемы отправки','op:failures')],
                     [b('Посмотреть как покупатель','home')]])

    def operator_order_buttons(self, o):
        oid=o['id']; rows=[]
        if o['status']=='requested': rows.append([b('Подготовить предложение','op:quote:'+oid)])
        if o['status'] in ('requested','quoted'): rows.append([b('Отказать с причиной','op:reject:'+oid)])
        if o['status']=='paid': rows.append([b('Выдать код / инструкцию','op:deliver:'+oid)])
        if o['status'] in ('paid','delivery_queued','delivered','refund_pending'):
            rows.append([b('Оформить возврат','op:refund:'+oid)])
        rows.append([b('Обновить статус','order:'+oid)])
        return rows

    def operator_state(self, uid):
        r=self.s.one('SELECT * FROM operator_drafts WHERE user_id=?',(uid,))
        if r and r['created']>=self.now()-1800:return json.loads(r['data'])
        self.s.run('DELETE FROM operator_drafts WHERE user_id=?',(uid,))
        return None

    def save_operator(self, uid, data):
        data['nonce']=secrets.token_hex(6)
        self.s.run('INSERT INTO operator_drafts VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
                   'data=excluded.data,created=excluded.created',(uid,json.dumps(data),self.now()))

    def operator_begin(self, uid, action, oid):
        self.admin(uid)
        o=self.own_order(uid,oid)
        allowed={'quote':('requested',),'deliver':('paid',),'reject':('requested','quoted')}
        if o['status'] not in allowed[action]:fail('Статус заказа изменился. Открой заказ заново.')
        if action=='quote' and not self.cfg.payments_enabled:
            fail('Приём оплаты ещё не включён. Сначала нужны данные продавца и подтверждённое исполнение заказа.')
        self.reset_flows(uid)
        d={'action':action,'oid':oid,'stage':'price' if action=='quote' else 'body'}
        self.save_operator(uid,d)
        prompt={'quote':'Введи итоговую цену в Telegram Stars: целое число от 1 до 100 000.',
                'deliver':'Пришли код или инструкцию подключения. До 1 800 символов. Перед отправкой покупателю покажу предпросмотр.',
                'reject':'Напиши причину отказа. От 3 до 500 символов. Сначала покажу предпросмотр.'}[action]
        self.screen(uid,f'<b>{e(o["title"])}</b>\nЗаказ {oid}\n\n{prompt}',[[b('Отмена','op:dashboard')]])

    def operator_callback(self, uid, data):
        if data.startswith('op:'):
            self.admin(uid)
            parts=data.split(':'); action=parts[1]
            if action=='dashboard':self.operator_dashboard(uid)
            elif action in ('stats','failures'):
                self.reset_flows(uid); self.admin_command(uid,'/'+action,'')
            elif action=='tickets':self.list_tickets(uid,admin=True)
            elif action=='filter' and len(parts)==3:
                if parts[2] not in ('requested','paid'):fail('Неизвестный список.')
                rows=self.s.all('SELECT * FROM orders WHERE status=? ORDER BY created LIMIT 20',(parts[2],))
                self.screen(uid,'<b>Заказы</b>\n'+('Выбери заказ.' if rows else 'Пока нет заказов.'),
                    [[b(o['title']+' · '+o['id'],'order:'+o['id'])] for o in rows]+[[b('← Управление','op:dashboard')]])
            elif action in ('quote','deliver','reject') and len(parts)==3:self.operator_begin(uid,action,parts[2])
            elif action=='refund' and len(parts)==3:self.admin_command(uid,'/refund',parts[2])
            elif action=='confirm' and len(parts)==3:
                d=self.operator_state(uid)
                if not d or d['nonce']!=parts[2] or d['stage']!='confirm':fail('Этот предпросмотр уже закрыт. Открой заказ заново.')
                if d['action']=='reply':
                    t=self.ticket_for(uid,d['tid'])
                    if t['status']!='open':fail('Обращение уже закрыто.')
                    self.ticket_message(d['tid'],'support',d['body'])
                    self.say(t['user_id'],f'<b>Ответ поддержки · {e(d["tid"])}</b>\n\n{e(d["body"])}',
                             [[b('Ответить','ticketreply:'+d['tid'])],[b('Обращение решено','ticketclose:'+d['tid'])]])
                    self.screen(uid,'Ответ отправлен покупателю.',[[b('← Обращения','op:tickets')]])
                else:
                    args=d['oid']+' '+(str(d['price'])+' ' if d['action']=='quote' else '')+d['body']
                    self.admin_command(uid,'/'+d['action'],args)
                self.s.run('DELETE FROM operator_drafts WHERE user_id=?',(uid,))
            elif action=='reply' and len(parts)==3:
                t=self.ticket_for(uid,parts[2])
                if t['status']!='open':fail('Обращение уже закрыто.')
                self.reset_flows(uid)
                self.save_operator(uid,{'action':'reply','stage':'body','tid':t['id']})
                self.screen(uid,'Напиши ответ покупателю: до 1 500 символов. Перед отправкой будет предпросмотр.',[[b('Отмена','op:dashboard')]])
            else:fail('Открой управление заново: /admin')
            return True
        if data=='tickets':self.list_tickets(uid); return True
        if data.startswith(('ticketnew:','ticketreply:','ticketview:','ticketclose:')):
            action,ref=data.split(':',1)
            if action=='ticketnew':
                if ref!='-':self.own_order(uid,ref)
                self.reset_flows(uid)
                self.save_selection(uid,{'mode':'support','order_id':None if ref=='-' else ref})
                self.screen(uid,'<b>Чем помочь?</b>\nНапиши вопрос одним сообщением — до 1 500 символов. '
                            'Не присылай пароли, коды входа и данные банковской карты.',[[b('Отмена','support')]])
            elif action=='ticketreply':
                t=self.ticket_for(uid,ref)
                if t['status']!='open':fail('Обращение закрыто. Можно создать новое в поддержке.')
                self.reset_flows(uid)
                self.save_selection(uid,{'mode':'support','tid':ref,'order_id':t['order_id']})
                self.screen(uid,'Напиши сообщение для поддержки: до 1 500 символов.',[[b('Отмена','support')]])
            elif action=='ticketview':self.show_ticket(uid,ref)
            elif action=='ticketclose':
                self.ticket_for(uid,ref)
                self.s.run("UPDATE tickets SET status='closed' WHERE id=?",(ref,))
                self.show_ticket(uid,ref)
            return True
        return False

    def operator_message(self, uid, text):
        selection=self.selection(uid)
        if selection and selection.get('mode')=='support':
            if not 3<=len(text.strip())<=1500:fail('Напиши вопрос текстом: от 3 до 1 500 символов.')
            tid=selection.get('tid')
            if tid:
                t=self.ticket_for(uid,tid)
                if t['status']!='open':fail('Обращение закрыто. Создай новое.')
            else:
                count=self.s.one("SELECT count(*) FROM tickets WHERE user_id=? AND status='open'",(uid,))[0]
                if count>=3:fail('У тебя уже 3 открытых обращения. Продолжи одно из них в разделе поддержки.')
                tid=uuid.uuid4().hex[:10]
                self.s.run('INSERT INTO tickets(id,user_id,order_id,created) VALUES (?,?,?,?)',
                           (tid,uid,selection.get('order_id'),self.now()))
            self.ticket_message(tid,'buyer',text.strip())
            self.clear_selection(uid)
            self.say(uid,f'<b>Обращение {tid} принято</b>\nОтвет придёт прямо в этот чат.',[[b('Моё обращение','ticketview:'+tid)]])
            self.say(self.cfg.owner_id,f'<b>Вопрос покупателя · {tid}</b>\nЗаказ: {e(selection.get("order_id") or "не указан")}\n\n{e(text.strip())}',
                     [[b('Ответить','op:reply:'+tid),b('История','ticketview:'+tid)]])
            return True
        if uid!=self.cfg.owner_id:return False
        d=self.operator_state(uid)
        if not d:return False
        text=text.strip()
        if d['stage']=='price':
            if not text.isascii() or not text.isdigit() or not 1<=int(text)<=100000:fail('Нужно целое число Stars от 1 до 100 000.')
            d.update(price=int(text),stage='body'); self.save_operator(uid,d)
            self.screen(uid,'<b>Что получит покупатель?</b>\nУкажи точный тариф, срок, страну, способ подключения и время выдачи. '
                        'От 20 до 500 символов.',[[b('Отмена','op:dashboard')]])
        elif d['stage']=='body':
            limits={'quote':(20,500),'deliver':(1,1800),'reject':(3,500),'reply':(3,1500)}
            low,high=limits[d['action']]
            if not low<=len(text)<=high:fail(f'Текст должен содержать от {low} до {high} символов.')
            d.update(body=text,stage='confirm'); self.save_operator(uid,d)
            price=f'Итого: {d["price"]} ⭐\n\n' if d['action']=='quote' else ''
            self.screen(uid,f'<b>Проверь перед отправкой</b>\n\n{price}{e(text)}',
                        [[b('Подтвердить и отправить','op:confirm:'+d['nonce'])],[b('Отмена','op:dashboard')]])
        else:self.screen(uid,'Подтверди предпросмотр кнопкой или отмени действие.',[[b('Отмена','op:dashboard')]])
        return True

    def ticket_for(self, uid, tid):
        t=self.s.one('SELECT * FROM tickets WHERE id=?',(tid,))
        if not t or (t['user_id']!=uid and uid!=self.cfg.owner_id):fail('Обращение не найдено.')
        return t

    def ticket_message(self, tid, author, body):
        self.s.run('INSERT INTO ticket_messages(ticket_id,author,body,created) VALUES (?,?,?,?)',(tid,author,body,self.now()))

    def list_tickets(self, uid, admin=False):
        self.reset_flows(uid)
        if admin:
            self.admin(uid)
            ts=self.s.all("SELECT * FROM tickets WHERE status='open' ORDER BY created LIMIT 20")
        else:ts=self.s.all('SELECT * FROM tickets WHERE user_id=? ORDER BY created DESC LIMIT 15',(uid,))
        rows=[[b(t['id']+' · '+('Открыто' if t['status']=='open' else 'Решено'),'ticketview:'+t['id'])] for t in ts]
        rows.append([b('← Управление','op:dashboard')] if admin else [b('Задать вопрос','ticketnew:-'),b('Главная','home')])
        self.screen(uid,'<b>Обращения</b>\n'+('Выбери обращение.' if ts else 'Пока обращений нет.'),rows)

    def show_ticket(self, uid, tid):
        t=self.ticket_for(uid,tid)
        messages=self.s.all('SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id DESC LIMIT 2',(tid,))
        body=f'<b>Обращение {e(tid)}</b>\n'+('Открыто' if t['status']=='open' else 'Решено')
        if t['order_id']:body+='\nЗаказ: '+e(t['order_id'])
        body+='\n\nПоследние сообщения:\n'
        for m in reversed(messages):body+='\n<b>'+('Покупатель' if m['author']=='buyer' else 'Поддержка')+'</b>\n'+e(m['body'])+'\n'
        rows=[]
        if t['status']=='open':
            rows.append([b('Ответить',('op:reply:' if uid==self.cfg.owner_id else 'ticketreply:')+tid)])
            rows.append([b('Вопрос решён','ticketclose:'+tid)])
        rows.append([b('← Все обращения','op:tickets' if uid==self.cfg.owner_id else 'tickets')])
        self.screen(uid,body,rows)
