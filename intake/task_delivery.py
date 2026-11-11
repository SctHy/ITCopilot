"""Configured outbound transports; never accept arbitrary endpoints from browser input."""
import asyncio
import json
import os
import smtplib
from email.message import EmailMessage
from urllib.parse import urlencode


class EmailTaskDelivery:
    def __init__(self, host, port, sender, base_url, username='', password=''):
        self.host, self.port, self.sender, self.base_url = host, port, sender, base_url
        self.username, self.password = username, password

    async def send(self, recipient, text, task_id, delivery_id):
        message = EmailMessage()
        message['From'], message['To'], message['Subject'] = self.sender, recipient, text
        message['Message-ID'] = '<' + delivery_id + '@it-copilot>'
        message.set_content(text + '\n' + self.base_url + '?' + urlencode({'task': task_id}))

        def deliver():
            with smtplib.SMTP(self.host, self.port, timeout=15) as client:
                client.starttls()
                if self.username:
                    client.login(self.username, self.password)
                client.send_message(message)
        operation = asyncio.create_task(asyncio.to_thread(deliver))
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Do not requeue a timed-out send while its SMTP thread is still alive.
            await asyncio.gather(operation, return_exceptions=True)
            raise


class FeishuTaskDelivery:
    def __init__(self, client, base_url):
        self.client, self.base_url = client, base_url

    async def send(self, recipient, text, task_id, delivery_id):
        await self.client._call('POST', '/im/v1/messages', params={'receive_id_type': 'open_id'}, json={
            'receive_id': recipient, 'msg_type': 'text', 'uuid': delivery_id[:50],
            'content': json.dumps({'text': text + '\n' + self.base_url + '?' + urlencode({'task': task_id})}, ensure_ascii=False)})


def configured_task_delivery(feishu=None):
    contacts = json.loads(os.getenv('IT_COPILOT_TASK_CONTACTS', '{}'))
    base = os.getenv('IT_COPILOT_PUBLIC_WEB_URL', 'http://127.0.0.1:5173').rstrip('/')
    transports = {}
    if os.getenv('IT_COPILOT_SMTP_HOST') and os.getenv('IT_COPILOT_SMTP_FROM'):
        transports['email'] = EmailTaskDelivery(os.environ['IT_COPILOT_SMTP_HOST'], int(os.getenv('IT_COPILOT_SMTP_PORT', '587')),
            os.environ['IT_COPILOT_SMTP_FROM'], base, os.getenv('IT_COPILOT_SMTP_USERNAME', ''), os.getenv('IT_COPILOT_SMTP_PASSWORD', ''))
    if feishu:
        transports['feishu'] = FeishuTaskDelivery(feishu, base)
    return contacts, transports
