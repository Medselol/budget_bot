"""Readable construction ledger PDF, with embedded Cyrillic fonts."""
from collections import defaultdict
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, LongTable, PageBreak

INK = '#172B3A'
GREEN = '#148566'
RED = '#D45454'
BLUE = '#426BBA'


def pdf_report(rows, start, end, project, scope='Личный отчёт'):
    rows = [dict(r) for r in rows]
    for name, file in [('Ledger', 'LedgerSans.ttf'), ('LedgerBold', 'LedgerSans-Bold.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(Path(__file__).parent / 'fonts' / file)))
    pdfmetrics.registerFontFamily('Ledger', normal='Ledger', bold='LedgerBold', italic='Ledger', boldItalic='LedgerBold')
    styles = {
        'body': ParagraphStyle('body', fontName='Ledger', fontSize=10, leading=15, textColor=colors.HexColor(INK)),
        'small': ParagraphStyle('small', fontName='Ledger', fontSize=9, leading=13, textColor=colors.HexColor('#647681')),
        'title': ParagraphStyle('title', fontName='LedgerBold', fontSize=24, leading=30, textColor=colors.HexColor(INK), spaceAfter=10),
        'h': ParagraphStyle('h', fontName='LedgerBold', fontSize=15, leading=21, textColor=colors.HexColor(INK), spaceBefore=16, spaceAfter=10, keepWithNext=True),
    }
    def p(value, style='body'):
        return Paragraph(escape(str(value or '')).replace('\n', '<br/>'), styles[style])
    def amount(value, currency):
        return f'{Decimal(value) / 100:,.2f}'.replace(',', ' ') + ' ' + currency
    def table(data, widths, header=False):
        t = LongTable(data, colWidths=widths, repeatRows=1 if header else 0, splitInRow=1, hAlign='LEFT')
        rules = [('VALIGN',(0,0),(-1,-1),'TOP'), ('LEFTPADDING',(0,0),(-1,-1),10), ('RIGHTPADDING',(0,0),(-1,-1),10), ('TOPPADDING',(0,0),(-1,-1),9), ('BOTTOMPADDING',(0,0),(-1,-1),9), ('ROWBACKGROUNDS',(0,1 if header else 0),(-1,-1),[colors.HexColor('#F1F5F7'), colors.white]), ('LINEBELOW',(0,0),(-1,-1),0.3,colors.HexColor('#E1E8EC'))]
        if header:
            rules.append(('BACKGROUND',(0,0),(-1,0),colors.HexColor('#E1EBEF')))
        t.setStyle(TableStyle(rules))
        return t
    width = 511
    stream = BytesIO()
    doc = SimpleDocTemplate(stream, pagesize=(595,842), rightMargin=42,leftMargin=42,topMargin=44,bottomMargin=44, title='Отчёт по стройке', author='Учёт стройки')
    story = [p('ФИНАНСЫ СТРОЙКИ', 'small'), Spacer(1,12), p('Отчёт по стройке','title'), p(project), p(f'{scope}  •  {start or "С начала учёта"} - {end or "Сегодня"}', 'small'), Spacer(1,14)]
    for currency in ('UAH','USD'):
        part = [r for r in rows if r['currency']==currency]
        income = sum(r['amount_kop'] for r in part if r['kind']=='income')
        expense = sum(r['amount_kop'] for r in part if r['kind']=='expense')
        story.append(p('Гривны / UAH' if currency=='UAH' else 'Доллары / USD','h'))
        cards=[]
        for label, val, color in [('↓ ПРИХОД',income,GREEN),('↑ РАСХОДЫ',expense,RED)]:
            size = min(19, 19 * 232 / max(1, pdfmetrics.stringWidth(amount(val,currency), 'LedgerBold', 19)))
            cards.append(Paragraph(f'<font color="{color}">{label}</font><br/><font name="LedgerBold" size="{size}">{amount(val,currency)}</font>', ParagraphStyle('card',parent=styles['body'],leading=28)))
        story.append(table([cards],[width/2]*2))
        story.append(Spacer(1,6))
        fx=sum((r['target_amount_kop'] if r.get('target_currency')==currency else 0)-(r['amount_kop'] if r['currency']==currency else 0) for r in rows if r.get('target_currency'))
        story.append(p('Обмен валют: '+amount(fx,currency)))
        story.append(p('Изменение денег: '+amount(income-expense+fx,currency)))
        story.append(p('Приход включает вложения, займы, оплаты покупателей и возвраты. Переводы в одной валюте не входят в итог. Обмен отражён отдельно.', 'small'))
        groups=defaultdict(int)
        for r in part:
            if r['kind']=='expense': groups[r.get('category') or 'Прочее']+=r['amount_kop']
        if groups:
            story.append(Spacer(1,8))
            data=[[p('Расходы по этапам','small'),p('Сумма','small'),p('Доля','small')]]
            for label,val in sorted(groups.items(), key=lambda x:-x[1]):
                data.append([p(label),p(amount(val,currency)),p(f'{val/expense:.0%}')])
            story.append(table(data,[261,170,80],True))
    if rows:
        story.extend([PageBreak(), p('Динамика по месяцам','title')])
        story.append(p('Приход и расходы за выбранный период. Переводы между участниками исключены.', 'small'))
        for currency in ('UAH','USD'):
            months = defaultdict(lambda: [0,0])
            for r in rows:
                if r['currency'] == currency and r['kind'] in ('income','expense'):
                    months[r['occurred_on'][:7]][0 if r['kind']=='income' else 1] += r['amount_kop']
            if not months: continue
            story.append(p(currency, 'h'))
            data = [[p('Месяц','small'),p('Приход','small'),p('Расходы','small'),p('Приход − расход','small')]]
            for month,(inc,exp) in sorted(months.items()):
                data.append([p(month),p(amount(inc,currency)),p(amount(exp,currency)),p(amount(inc-exp,currency))])
            story.append(table(data,[76,145,145,145],True))
    if not rows:
        story.extend([Spacer(1,20),p('За выбранный период операций нет.')])
    else:
        story.extend([PageBreak(),p('Журнал операций','title'),p(f'Всего записей: {len(rows)}','small'),Spacer(1,12)])
        data=[[p('Дата / участник','small'),p('Операция и назначение','small'),p('Сумма','small')]]
        for r in rows:
            kind=r['kind']
            label,color={'income':('↓ Приход',GREEN),'expense':('↑ Расход',RED),'transfer':('↔ Перевод',BLUE)}[kind]
            if r.get('target_currency'): label='↔ Обмен валют'
            detail=[f'<font name="LedgerBold" color="{color}">{label}</font>']
            if kind=='transfer':
                detail.append(escape(f'{r.get("from_account") or ""} → {r.get("to_account") or ""}'))
            else:
                detail.append(escape(r.get('category') or r.get('source') or 'Прочее'))
                if r.get('cost_type'): detail.append(escape(r['cost_type']))
                if r.get('account'): detail.append('Счёт: '+escape(r['account']))
            if r.get('target_currency'): detail.append(escape('Курс: 1 USD = '+r['exchange_rate']+' UAH'))
            if r.get('receipt_label'): detail.append(escape(r['receipt_label']))
            if r.get('receipt_url'):
                detail.append('<link href="'+escape(r['receipt_url'], {'"':'&quot;'})+'" color="'+BLUE+'"><u>Открыть чеки / операцию</u></link>')
            if r.get('project'): detail.append(escape(r['project']))
            if r.get('comment'): detail.append(escape(r['comment']).replace('\n','<br/>'))
            data.append([p(f'{r["occurred_on"]}\n{r.get("user_name") or "Участник"}\n№ {r["id"]}'),Paragraph('<br/>'.join(detail),styles['body']),p(amount(r['amount_kop'],r['currency']) + (' → '+amount(r['target_amount_kop'],r['target_currency']) if r.get('target_currency') else ''))])
        story.append(table(data,[111,245,155],True))
    def footer(canvas, document):
        canvas.setStrokeColor(colors.HexColor('#DCE5E9')); canvas.line(42,34,553,34)
        canvas.setFont('Ledger',8);canvas.setFillColor(colors.HexColor('#647681'))
        canvas.drawString(42,22,'Учёт стройки • Финансовый отчёт')
        canvas.drawRightString(553,22,f'Страница {document.page}')
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    return stream.getvalue()
