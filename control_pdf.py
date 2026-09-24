"""Printable, Cyrillic plan/actual report for the construction site."""
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,LongTable,TableStyle,PageBreak
import bot as ledger
import control_data as data


def budget_pdf(db):
    for name,file in [('Ledger','LedgerSans.ttf'),('LedgerBold','LedgerSans-Bold.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames(): pdfmetrics.registerFont(TTFont(name,str(Path(__file__).parent/'fonts'/file)))
    normal=ParagraphStyle('normal',fontName='Ledger',fontSize=9,leading=13,textColor=colors.HexColor('#183640'))
    heading=ParagraphStyle('heading',parent=normal,fontName='LedgerBold',fontSize=21,leading=27,spaceAfter=14)
    def p(s): return Paragraph(escape(str(s)),normal)
    out=BytesIO();story=[]
    for index,currency in enumerate(ledger.CURRENCIES):
        if index: story.append(PageBreak())
        rows=data.budget_rows(db,currency)
        story += [Paragraph('Бюджет: план / факт',heading),p(ledger.DEFAULT_PROJECT_NAME),p(f'{currency} · С начала учёта · {data.today().isoformat()}'),Spacer(1,14)]
        table=[[p(x) for x in ('Этап','План','Факт','Остаток / перерасход')]]
        for stage,plan,fact in rows:
            delta='План не задан' if plan is None else ('Перерасход '+ledger.money(fact-plan,currency) if fact>plan else 'Остаток '+ledger.money(plan-fact,currency))
            table.append([p(stage),p('Не задан' if plan is None else ledger.money(plan,currency)),p(ledger.money(fact,currency)),p(delta)])
        t=LongTable(table,colWidths=[142,112,112,145],repeatRows=1,splitInRow=1)
        t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('BACKGROUND',(0,0),(-1,0),colors.HexColor('#DAECE7')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F3F6F5')]),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
        story += [t,Spacer(1,14),p('Сумма заданных планов: '+ledger.money(sum(plan or 0 for _,plan,_ in rows),currency)),p('Фактические расходы: '+ledger.money(sum(fact for _,_,fact in rows),currency)),p('Без заданного плана: '+ledger.money(sum(fact for _,plan,fact in rows if plan is None),currency)),Spacer(1,8),p('План общий для всех участников этого бота. Факт включает все сохранённые расходы, в том числе архивных участников. Переводы исключены. Обязательства без оплаты не входят в факт.')]
    def footer(c,doc):
        c.setFont('Ledger',9);c.drawRightString(553,22,str(doc.page))
    SimpleDocTemplate(out,pagesize=(595,842),leftMargin=42,rightMargin=42,topMargin=38,bottomMargin=40,title='Бюджет план / факт').build(story,onFirstPage=footer,onLaterPages=footer)
    return out.getvalue()
