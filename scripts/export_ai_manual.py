"""Export the complete local HTML user guide as one chatbot attachment.

Run from any directory: python scripts/export_ai_manual.py
No third-party packages or application/operational data access are required.
"""
from __future__ import annotations
import ast
import json
import posixpath
import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'AI_사용설명서.txt'

class Sections(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []
        self.current = None
        self.heading = False
        self.ignore = []
        self.lists = []
        self.link = None
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.ignore:
            if tag == self.ignore[-1]: self.ignore.append(tag)
            return
        if tag in {'figure', 'nav', 'script', 'style', 'footer'}:
            self.ignore.append(tag)
            return
        if tag == 'section':
            if self.current is not None: raise ValueError('Nested sections need explicit handling')
            self.current = {'id':attrs['id'], 'title':'', 'parts':[], 'links':[]}
        if self.current is None: return
        parts = self.current['parts']
        if tag == 'h2': self.heading = True
        if tag in {'h2','h3','h4','h5','h6','p','aside','ol','ul','table'}: parts.append('\n\n')
        if tag == 'a': self.link = attrs.get('href')
        if tag in {'ol','ul'}: self.lists.append([tag, int(attrs.get('start', '1')) - 1])
        if tag == 'li':
            if self.lists:
                self.lists[-1][1] += 1
                marker = str(self.lists[-1][1])+'. ' if self.lists[-1][0]=='ol' else '- '
            else: marker = '- '
            parts.append('\n'+marker)
        if tag == 'tr': parts.append('\n')
        if tag == 'br': parts.append('\n')
    def handle_endtag(self, tag):
        if self.ignore:
            if tag == self.ignore[-1]: self.ignore.pop()
            return
        if self.current is None: return
        if tag == 'h2': self.heading = False
        if tag in {'h2','h3','h4','h5','h6','p','aside','ol','ul','table'}: self.current['parts'].append('\n\n')
        if tag == 'a' and self.link is not None:
            index = len(self.current['links'])
            self.current['links'].append(self.link)
            self.current['parts'].append(f'@@MANUAL_LINK_{index}@@')
            self.link = None
        if tag in {'td','th'}: self.current['parts'].append(' | ')
        if tag in {'ol','ul'} and self.lists: self.lists.pop()
        if tag == 'section':
            raw = ''.join(self.current.pop('parts'))
            lines = [re.sub(r'[\t \r\f\v]+',' ',line).strip().rstrip('|').rstrip() for line in raw.split('\n')]
            self.current['text'] = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
            self.items.append(self.current)
            self.current = None
    def handle_data(self, data):
        if self.current is not None and not self.ignore:
            self.current['parts'].append(data)
            if self.heading: self.current['title'] += data

AI_GUIDANCE = '''급식 배송 운영 — AI 사용 설명서
대상: Windows 실행 파일을 사용하는 업무 담당자와 사용법을 안내하는 챗봇
기준 버전: {version} / 내용 확인일: 2026-11-10

이 파일 하나에 HTML 설명서의 모든 사용 절차와 상황별 찾아보기를 담았습니다.
실행 파일·코드·다른 설명서·스크린샷을 함께 첨부하지 않아도 본문을 읽을 수 있습니다.

[사용자가 챗봇에 처음 보낼 말]
첨부한 설명서 기준으로 급식 배송 운영 프로그램 사용법을 알려줘.
내가 하고 싶은 일을 말하면 필요한 메뉴 위치, 누르는 순서, 입력할 값,
저장 후 확인할 내용과 주의점을 쉬운 한국어로 설명해줘.
예: “다음 주 월요일부터 목요일까지 한 사람의 석식만 쉬게 하고 싶어.”

[이 파일을 읽는 챗봇의 답변 지침]
1. 이 문서는 프로그램 사용자 안내 자료다. 코드 수정·개발·빌드 안내로 답하지 않는다.
2. 사용자의 표현을 아래 ‘하고 싶은 일로 찾기’와 본문에서 찾는다. 똑같은 질문이 없더라도 관련 절차를 조합한다.
3. 조작 질문에는 ‘여는 위치 → 번호를 붙인 조작 순서 → 저장 후 확인’ 순으로 답한다. 처음 답변은 보통 3~5단계로 짧게 쓰되 필수 단계는 빼지 않는다. 필요한 주의점만 1~2개 덧붙이고 메뉴·버튼은 본문 이름을 그대로 쓴다. 뜻이나 차이를 묻는 질문은 결론과 짧은 예시부터 답한다.
4. 이미 알려준 정보는 다시 묻지 않는다. 대상자·가구·전체 중 범위, 식종·날짜 또는 급식·행복도시락 양식에 따라 답이 달라지고 아직 알 수 없을 때만 필요한 질문 1~2개를 먼저 한다. 양식 확인 위치는 ‘설정 → 문서 → 문서 양식’이다. 답을 기다리는 동안 공통으로 할 수 있는 단계만 안내한다.
5. 시작일·종료일·조회일·실제 배송일·식사일을 구분한다. 미래 변경을 저장하고 과거 날짜에서 확인하는 실수를 안내한다. 날짜를 임의로 정하지 않는다.
6. 화면만 바꾸는 동작과 실제 명단·일정·출력물을 바꾸는 동작을 구분한다. 삭제·중식 대상자 교체·복구·자동 파일 삭제에서는 관련 영향 범위를 설명한다.
7. 같은 결과에 가까운 기능이라도 서로 대체하지 않는다. 중지는 행 삭제가 아니며, 시트 삭제는 명단 삭제가 아니고, 화면 정렬은 배송순서 변경이 아니다.
8. 사용자 화면이나 실제 저장 자료를 보고 있는 것처럼 말하지 않는다. 사용자가 직접 눌러야 할 절차를 안내하고 저장·생성을 대신 수행했다고 말하지 않는다.
9. 본문에 없는 버튼·기능·자동 처리·복구 방법은 만들지 않는다. 문서로 판단할 수 없으면 확인할 수 없다고 말하고 화면 이름·안내 문구 또는 개인정보를 가린 화면을 요청한다. 코드 제공을 요구하지 않는다.
10. 다른 버전의 화면이면 차이를 알리고 현재 보이는 메뉴를 확인한다. 인터넷의 다른 프로그램 설명보다 이 버전의 본문을 우선한다.
11. 본문의 예시는 실제 대상자가 아니다. 사용법 설명에 전체 명단이나 비밀번호를 첨부하게 하지 않는다.
12. 본문의 ‘[참고: S번호 · 제목]’은 이 파일 안의 관련 절이다. 필요한 절까지 읽어 조건을 확인하고 답변 끝에는 사용한 본문 번호와 제목을 짧게 적는다. 예: ‘근거: {pause_reference}’. 실제 번호는 아래 목차를 따른다.

[기본 용어와 혼동하기 쉬운 구분]
- 식종: 석식·조식·중식. 한 사람이 여러 식종을 받을 수 있고 상태·담당·순서는 식종별일 수 있다.
- 조회일: 명단 화면 왼쪽에서 보고 있는 날짜. 적용일은 변경이 실제로 시작되는 날짜다.
- 식사일: 제공하는 식사의 원래 날짜. 실제 배송일: 묶음배송을 포함해 물건이 나가는 날.
- 가구: 같이 묶어 관리하는 사람들. 공용 칼럼은 해당 가구원이 값을 함께 쓰는 항목이고 공용 예외는 개인별 값을 허용한다.
- 일일식수: 날짜별 집계표. 급식은 실제 배송 식수·가구 수, 행복도시락은 선택일의 가입·상태 기준 인원이다. 행복도시락은 일시중지도 포함하고 3일분 조식도 1명으로 센다. 청구서·확인서의 식사 수와 혼동하지 않는다.
- 배송목록: 기사별 대상 목록. 급식은 실제 배송 대상, 행복도시락은 가입·상태 기준의 1/빈칸 표시다. 일부 화면·양식에서는 배송대타라고 표시한다.
- 변경번호: 저장한 변경 기록의 번호. 과거 원본으로 문서를 만들거나 이전 상태를 조회할 때 사용한다.
- 작업본: 같은 운영 파일 안에서 별도로 이어가는 명단·상태·설정. 전체 변경 이력에서 만들거나 전환하며, 시트는 같은 명단의 화면 배치이므로 작업본과 다르다. 기존 화면 예시에서는 분기라고 표시될 수 있다.
- 불러오기: Excel 등 입력 파일의 내용을 반영. 내보내기: 현재 자료를 Excel로 출력. 기존 화면 예시에는 불러오기가 가져오기로 표시될 수 있다.
- 운영 파일 비밀번호: 앱의 명단 파일을 여는 비밀번호. 생성 Excel 비밀번호: 새로 만드는 보호 Excel의 공통 비밀번호. 원본 Excel의 열기 비밀번호: 받은 파일을 읽을 때 필요하다.
- 생성 후 명단·설정을 수정해도 이미 만든 Excel 파일은 자동으로 바뀌지 않는다. 변경 내용으로 새 파일을 만든다.

'''

def main():
    pages = [ROOT/'설명서.html', *sorted((ROOT/'html').glob('[0-9][0-9]-*.html'))]
    sections = []
    for page in pages:
        parser = Sections(); parser.feed(page.read_text(encoding='utf-8-sig'))
        for item in parser.items:
            if page.name=='설명서.html' and item['id'] in {'daily','contents','ai','questions'}: continue
            item['source'] = page.relative_to(ROOT).as_posix()+'#'+item['id']
            item['code'] = f'S{len(sections)+1:03d}'
            sections.append(item)
    lookup = {item['source']:item for item in sections}
    if len(lookup)!=len(sections): raise ValueError('Duplicate section anchors')
    # Keep HTML cross-references usable in a standalone text attachment.
    for item in sections:
        source_page = item['source'].split('#', 1)[0]
        for index, href in enumerate(item.pop('links')):
            url = urlsplit(href)
            reference = ''
            if not url.scheme and not url.netloc and url.fragment:
                target_page = posixpath.normpath(posixpath.join(
                    posixpath.dirname(source_page), unquote(url.path))) if url.path else source_page
                target = lookup.get(target_page + '#' + unquote(url.fragment))
                if target is not None and target is not item:
                    reference = f' [참고: {target["code"]} · {target["title"]}]'
            item['text'] = item['text'].replace(f'@@MANUAL_LINK_{index}@@', reference)
    intents = json.loads((ROOT/'docs/manual-intents.json').read_text(encoding='utf-8-sig'))
    missing = set(intents)-lookup.keys()
    if missing: raise ValueError(f'Missing question targets: {sorted(missing)}')
    version_tree = ast.parse((ROOT/'src/version.py').read_text(encoding='utf-8-sig'))
    version = next(ast.literal_eval(n.value) for n in version_tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='APP_VERSION' for t in n.targets))
    pause = lookup['html/01-status.html#pause']
    parts = [AI_GUIDANCE.format(version=version, pause_reference=f'{pause["code"]} · {pause["title"]}'),'[하고 싶은 일로 찾기]\n']
    html_rows = []
    count = 0
    for target, questions in intents.items():
        item=lookup[target]
        for question in questions:
            count += 1
            parts.append(f'{count}. {question} → {item["code"]} · {item["title"]}\n')
        html_target = target.replace('설명서.html#','#')
        html_rows.append('<tr><td>'+ '<br>'.join(escape(q) for q in questions)+'</td><td><a href="'+escape(html_target,quote=True)+'">'+escape(item['title'])+'</a></td></tr>')
    parts.append('\n[전체 본문 목차]\n')
    parts.extend(f'{s["code"]} · {s["title"]}\n' for s in sections)
    parts.append('\n[전체 사용법]\n')
    parts.extend('\n'+'='*60+'\n'+s['code']+' · '+s['title']+'\n'+'='*60+'\n'+s['text']+'\n' for s in sections)
    parts.append('\n[설명서 끝]\n')
    OUTPUT.write_text(''.join(parts),encoding='utf-8')
    # The same intent index is available to people reading the HTML guide.
    page=ROOT/'설명서.html'; text=page.read_text(encoding='utf-8')
    block='<section id="questions"><h2>하고 싶은 일로 찾기</h2><p>질문과 가까운 항목의 설명을 누르세요. 같은 안내는 AI용 설명서에도 들어 있습니다.</p><div class="table-wrap"><table><thead><tr><th scope="col">하고 싶은 일</th><th scope="col">사용 방법</th></tr></thead><tbody>'+''.join(html_rows)+'</tbody></table></div></section>'
    if '<section id="questions">' in text:
        text=re.sub(r'<section id="questions">.*?</section>',lambda m:block,text,count=1,flags=re.S)
    else:
        text=text.replace('<section id="screen">',block+'<section id="screen">')
    if '<a href="#questions">' not in text:
        text=text.replace('<a href="#daily">','<a href="#questions">하고 싶은 일로 찾기</a><a href="#daily">',1)
    page.write_text(text,encoding='utf-8')
    print(json.dumps({'version':version,'sections':len(sections),'questions':count,'bytes':OUTPUT.stat().st_size},ensure_ascii=False))

if __name__=='__main__': main()
