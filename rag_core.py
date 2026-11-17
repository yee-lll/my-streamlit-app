# ==========================================================
# 1. 문서 정리 · 제목 인식 · 구조 기반 청킹
# ==========================================================
import hashlib
import json
import os
import re
from dataclasses import dataclass, field

FOOTER_RE = re.compile(r'^\s*AIVLE School 백서\s*·\s*\d+\s*$')
MARK_RE = re.compile(r'</?mark>')
HEADING_MD_RE = re.compile(r'^(#{1,6})\s+(.*)$')

# 번호로 제목 계층을 정한다. PyMuPDF4LLM의 '#' 개수는 글자 크기 추정이라 믿지 않는다.
HEADING_RULES = [
    (0, re.compile(r'^PART\s*\d+\.\s')),               # PART 1. / PART 2.
    (1, re.compile(r'^\d{1,2}\.\s+\S')),                # 1. 장
    (1, re.compile(r'^(부록\s*[A-C]\.|참고 자료$|목차$)')),
    (2, re.compile(r'^\d{1,2}\.\d{1,2}\s+\S')),         # 1.1 절
    (2, re.compile(r'^[A-C]\d\.\s+\S')),                # 부록 A1. B1.
    (3, re.compile(r'^(Q\d*\.|전략\s*\d+\.|\(\d\)\s)')), # Q1. / Q. / 전략 1. / (1)
    (3, re.compile(r'^\[(안내|AI/DX공통)\]')),          # 부록 C 공지 제목
]
NOT_HEADING_RE = re.compile(r'^(출처|답변|설명|공식 기준|-|\d+\)|<|\[|\*|·|AIVLE School 백서)')
SKIP_SECTIONS = ('목차', '참고 자료')   # 검색 잡음이 되는 절은 색인에서 뺀다


def load_pages(filepath):
    from langchain_pymupdf4llm import PyMuPDF4LLMLoader
    try:
        import pymupdf
        pymupdf.set_messages(path=os.devnull)
    except Exception:
        pass
    return PyMuPDF4LLMLoader(filepath).load()


def clean_line(line):
    """하이라이트/코드 배경 태그, 굵게 표시, 이스케이프를 지운다."""
    line = MARK_RE.sub('', line)
    line = line.replace('**', '').replace('\\', '')
    return line.rstrip()


def heading_level(raw, from_markdown, last_chapter):
    """제목이면 (level, 새 장 번호), 아니면 (None, last_chapter)."""
    text = raw.replace('`', '').strip()
    text = re.sub(r'^[·•]\s*', '', text)
    if not text or len(text) > 90:
        return None, last_chapter
    for level, pattern in HEADING_RULES:
        if pattern.match(text):
            if level == 1 and text[0].isdigit():
                number = int(re.match(r'^(\d+)', text).group(1))
                if number != last_chapter + 1:        # 부록 안의 '1. 2.' 목록은 장이 아니다
                    return None, last_chapter
                return 1, number
            if level == 3 and not from_markdown and text.startswith('Q.'):
                return 3, last_chapter
            return level, last_chapter
    # 번호 없는 소제목: 로더가 제목으로 표시한 짧은 줄만 인정
    if (from_markdown and not NOT_HEADING_RE.match(text) and len(text) < 70
            and not text.endswith(('.', ']', ':')) and not re.search(r'[=({]', text)):
        return 3, last_chapter
    return None, last_chapter


def part_of(path):
    head = path[0] if path else ''
    if head.startswith('PART 1'):
        return '선배·교육생 경험'
    if head.startswith('PART 2'):
        return '공식 안내'
    if head.startswith('부록'):
        return '공식 원문(부록)'
    return '문서 안내'


@dataclass
class Section:
    path: list
    title: str
    page_start: int
    page_end: int
    lines: list = field(default_factory=list)

    @property
    def text(self):
        return '\n'.join(self.lines).strip()


def parse_sections(pages):
    """페이지별 마크다운(Document 목록)을 제목 계층이 붙은 절 목록으로 바꾼다."""
    sections, stack = [], {}
    current = Section(['문서 안내'], '표지', 1, 1)
    last_chapter, in_toc = -1, False
    for page_doc in pages:
        page = int(page_doc.metadata.get('page', 0)) + 1
        for raw_line in page_doc.page_content.splitlines():
            line = clean_line(raw_line)
            if not line.strip() or FOOTER_RE.match(line):
                continue
            match = HEADING_MD_RE.match(line)
            candidate = match.group(2) if match else line
            level, new_chapter = heading_level(candidate, bool(match), last_chapter)
            title = re.sub(r'^[·•]\s*', '', candidate.replace('`', '').strip())
            if title == '목차':
                in_toc = True
            if in_toc and level is not None and title != '목차':
                if level == 1 and title.startswith('0.') and current.title == '목차' and current.lines:
                    in_toc, new_chapter = False, 0      # 목차가 끝나고 0장이 시작됨
                else:
                    current.lines.append(title)
                    continue
            if level is None:
                current.lines.append(line)
                current.page_end = page
                continue
            last_chapter = new_chapter
            if current.lines or current.title:
                sections.append(current)
            if level == 0:
                stack = {0: title}
            elif level == 1:
                stack = {k: v for k, v in stack.items() if k < 1 and not title.startswith(('부록', '참고', '목차', '0.'))}
                stack[1] = title
            else:
                stack = {k: v for k, v in stack.items() if k < level}
                stack[level] = title
            path = [stack[k] for k in sorted(stack)]
            current = Section(path, title, page, page)
    sections.append(current)
    return [s for s in sections if s.text]


def _tokens(text, encoder):
    return len(encoder.encode(text))


def split_table(lines, max_tokens, encoder):
    """마크다운 표를 행 묶음으로 자르고, 각 묶음에 머리행을 반복한다."""
    header, rows = lines[:2], lines[2:]
    chunks, buf = [], []
    for row in rows:
        if buf and _tokens('\n'.join(header + buf + [row]), encoder) > max_tokens:
            chunks.append('\n'.join(header + buf))
            buf = []
        buf.append(row)
    if buf or not chunks:
        chunks.append('\n'.join(header + buf))
    return chunks


def chunk_section(section, encoder, splitter, max_tokens):
    """절 하나를 청크 목록으로 만든다. 표는 표 단위, FAQ 문항은 문항 단위로 유지한다."""
    text = section.text
    if _tokens(text, encoder) <= max_tokens:
        return [text]
    blocks, table = [], []
    for line in section.lines:
        if line.lstrip().startswith('|'):
            table.append(line)
            continue
        if table:
            blocks.append(('table', table)); table = []
        blocks.append(('text', [line]))
    if table:
        blocks.append(('table', table))
    pieces, buf = [], []

    def flush():
        if buf:
            pieces.extend(splitter.split_text('\n'.join(buf)))
            buf.clear()
    for kind, lines in blocks:
        if kind == 'table':
            flush()
            pieces.extend(split_table(lines, max_tokens, encoder))
        else:
            buf.extend(lines)
    flush()
    return [p for p in pieces if p.strip()]


def make_chunks(pages, source_name, chunk_tokens=500, overlap_tokens=80):
    """로더 결과 → 메타데이터가 붙은 LangChain Document 목록."""
    import tiktoken
    from langchain_core.documents import Document
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    encoder = tiktoken.get_encoding('cl100k_base')
    splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        encoding_name='cl100k_base', chunk_size=chunk_tokens, chunk_overlap=overlap_tokens,
        separators=['\nQ', '\n\n', '\n', '. ', ' ', ''])
    documents = []
    for section in parse_sections(pages):
        if section.title in SKIP_SECTIONS or (section.path and section.path[0] in SKIP_SECTIONS):
            continue
        path_text = ' > '.join(section.path)
        part = part_of(section.path)
        for index, body in enumerate(chunk_section(section, encoder, splitter, chunk_tokens)):
            content_hash = hashlib.sha256(f'{path_text}\n{body}'.encode('utf-8')).hexdigest()
            documents.append(Document(
                page_content=f'[{path_text}]\n{body}',
                metadata={
                    'chunk_id': 'c-' + content_hash[:12],
                    'content_hash': content_hash,
                    'part': part,
                    'section_path': path_text,
                    'title': section.title,
                    'page_start': section.page_start,
                    'page_end': section.page_end,
                    'piece': index,
                    'source': source_name,
                }))
    # 같은 내용이 두 번 나오면 하나만 남긴다 (ID 충돌 방지)
    unique, seen = [], set()
    for doc in documents:
        if doc.metadata['chunk_id'] not in seen:
            seen.add(doc.metadata['chunk_id'])
            unique.append(doc)
    return unique

# ==========================================================
# 2. RAG 파이프라인 함수
# ==========================================================
import contextlib
import contextvars
import copy
import time

from langchain_core.embeddings import Embeddings

from aivle_config import (CHAT_MODEL, CHUNK_TOKENS, EMBEDDING_MODEL, GRAPH_WEIGHT, MIN_GRAPH_COVERAGE,
                          OVERLAP_TOKENS, SYSTEM_PROMPT, TOP_K, USER_TEMPLATE)   # 사람이 정하는 설정

# 진행 상황 알림: 앱이 listener를 걸어 두면 각 단계의 시작·끝을 알려 준다 (결과에는 영향 없음)
_PROGRESS = contextvars.ContextVar('rag_progress', default=None)


@contextlib.contextmanager
def progress_listener(callback):
    token = _PROGRESS.set(callback)
    try:
        yield
    finally:
        _PROGRESS.reset(token)


def emit_progress(event, step, **info):
    callback = _PROGRESS.get()
    if callback is not None:
        try:
            callback(event, step, info)
        except Exception:
            pass                                  # 화면 갱신 실패가 답변을 막지 않게 한다


class CachedEmbeddings(Embeddings):
    """같은 텍스트는 다시 임베딩하지 않는다 (청크 해시 캐시). LangChain Embeddings 인터페이스."""

    def __init__(self, base, cache_dir='.embed_cache', namespace='default'):
        self.base, self.cache_dir, self.namespace = base, cache_dir, namespace
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, text):
        key = hashlib.sha256(f'{self.namespace}\n{text}'.encode('utf-8')).hexdigest()
        return os.path.join(self.cache_dir, key + '.json')

    def embed_documents(self, texts):
        vectors, missing = [None] * len(texts), []
        for i, text in enumerate(texts):
            path = self._path(text)
            if os.path.exists(path):
                with open(path, encoding='utf-8') as f:
                    vectors[i] = json.load(f)
            else:
                missing.append(i)
        if missing:
            new_vectors = self.base.embed_documents([texts[i] for i in missing])
            for i, vector in zip(missing, new_vectors):
                vectors[i] = list(vector)
                with open(self._path(texts[i]), 'w', encoding='utf-8') as f:
                    json.dump(vectors[i], f)
        self.last_new = len(missing)
        return vectors

    def embed_query(self, text):
        return self.base.embed_query(text)


_TOKENIZER = None


def korean_tokenizer():
    """BM25용 한국어 토크나이저 (프로세스에서 한 번만 만든다). kiwipiepy가 없으면 글자 2-gram으로 대체한다."""
    global _TOKENIZER
    if _TOKENIZER is None:
        _TOKENIZER = _make_tokenizer()
    return _TOKENIZER


def _make_tokenizer():
    try:
        from kiwipiepy import Kiwi
        kiwi = Kiwi()
        keep = ('N', 'V', 'X', 'SL', 'SN', 'SH', 'M')

        def tokenize(text):
            return [t.form.lower() for t in kiwi.tokenize(text) if t.tag.startswith(keep)]
        return tokenize
    except ImportError:
        def tokenize(text):
            text = re.sub(r'\s+', '', text.lower())
            return [text[i:i + 2] for i in range(max(len(text) - 1, 1))]
        return tokenize


class HybridRetriever:
    """벡터 검색 + BM25 (+ 그래프)를 RRF(순위 결합)로 합친다."""

    def __init__(self, documents, vectorstore, k=5, fetch_k=10, use_bm25=True, graph=None, graph_weight=1.0):
        self.documents, self.vectorstore = documents, vectorstore
        self.k, self.fetch_k, self.use_bm25 = k, fetch_k, use_bm25
        self.graph, self.graph_weight = graph, graph_weight
        self.by_id = {d.metadata['chunk_id']: d for d in documents}
        self.bm25 = None
        self.last_trace = {}
        if use_bm25:
            from rank_bm25 import BM25Okapi
            self.tokenize = korean_tokenizer()
            self.bm25 = BM25Okapi([self.tokenize(d.page_content) or ['_'] for d in documents])

    def with_graph(self, graph):
        """같은 벡터저장소·BM25를 공유하고 그래프만 바꾼 검색기 (켬/끔 전환용)."""
        clone = copy.copy(self)
        clone.graph, clone.last_trace = graph, {}
        return clone

    def invoke(self, query):
        ranked, trace = {}, {'vector': [], 'bm25': [], 'graph': [], 'graph_concepts': []}

        def add(doc, rank, source, weight=1.0):
            cid = doc.metadata['chunk_id']
            ranked.setdefault(cid, [doc, 0.0])[1] += weight / (60 + rank + 1)
            trace[source].append(cid)

        for rank, doc in enumerate(self.vectorstore.similarity_search(query, k=self.fetch_k)):
            add(self.by_id.get(doc.metadata['chunk_id'], doc), rank, 'vector')
        if self.bm25 is not None:
            scores = self.bm25.get_scores(self.tokenize(query) or ['_'])
            order = sorted(range(len(scores)), key=lambda i: -scores[i])[:self.fetch_k]
            for rank, i in enumerate(order):
                if scores[i] <= 0:
                    break
                add(self.documents[i], rank, 'bm25')
        if self.graph is not None:                      # 4장: 그래프가 찾은 청크도 순위 결합에 넣는다
            for rank, cid in enumerate(self.graph.search(query, limit=self.fetch_k)):
                if cid in self.by_id:
                    add(self.by_id[cid], rank, 'graph', self.graph_weight)
            trace['graph_concepts'] = self.graph.last_trace.get('concepts', [])
            trace['graph_concept_keys'] = self.graph.last_trace.get('keys', [])
        best = sorted(ranked.values(), key=lambda item: -item[1])[:self.k]
        self.last_trace = trace
        return [doc for doc, _ in best]


def format_docs(docs):
    """검색된 청크를 [청크ID | 근거 종류 | 쪽 | 절] 머리표와 함께 이어 붙인다."""
    blocks = []
    for d in docs:
        m = d.metadata
        page = f"p.{m['page_start']}" + (f"-{m['page_end']}" if m['page_end'] != m['page_start'] else '')
        blocks.append(f"[{m['chunk_id']} | {m['part']} | {page} | {m['title']}]\n{d.page_content}")
    return '\n\n'.join(blocks) if blocks else '(검색된 백서 내용 없음)'


def format_history(history, max_turns=3):
    if not history:
        return '(없음)'
    lines = []
    for message in history[-max_turns * 2:]:
        role = '사용자' if message.get('role') == 'user' else '도우미'
        lines.append(f"{role}: {str(message.get('content', ''))[:500]}")
    return '\n'.join(lines)


class RagIndex:
    """한 번 만든 색인(청크·벡터저장소·검색기·그래프). 체인은 이 색인으로 여러 번 가볍게 만든다."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def build_index(filepath, *, chunk_tokens=CHUNK_TOKENS, overlap_tokens=OVERLAP_TOKENS, k=TOP_K, use_bm25=True,
                persist_directory='chroma_db', cache_dir='.embed_cache', embeddings=None,
                embedding_model=None, graph_path=None, min_graph_coverage=MIN_GRAPH_COVERAGE, use_neo4j=True,
                neo4j_search=True, graph_weight=GRAPH_WEIGHT, progress=None):
    """PDF 읽기 → 텍스트 분할 → Embedding → 벡터저장소 → 기존 데이터 비우기 → 문서 추가 → 검색기."""
    from langchain_chroma import Chroma
    report = progress or (lambda message: None)
    timing, start = {}, time.time()

    # PDF 파일 읽기
    report('PDF를 읽고 있어요')
    pages = load_pages(filepath)
    timing['pdf'] = time.time() - start

    # 텍스트 분할 (제목 구조 기반)
    docs = make_chunks(pages, os.path.basename(filepath), chunk_tokens, overlap_tokens)
    if not docs:
        raise ValueError('PDF에서 텍스트를 추출하지 못했습니다.')

    # Embedding 모델 (같은 청크는 캐시에서 재사용)
    report(f'청크 {len(docs)}개를 임베딩하고 있어요')
    step = time.time()
    if embeddings is None:
        from langchain_openai import OpenAIEmbeddings
        embedding_model = embedding_model or os.getenv('OPENAI_EMBEDDING_MODEL', EMBEDDING_MODEL)
        embeddings = OpenAIEmbeddings(model=embedding_model)
    cached = CachedEmbeddings(embeddings, cache_dir, namespace=embedding_model or type(embeddings).__name__)

    # 벡터저장소 만들기
    vectorstore = Chroma(collection_name='aivle_docs', embedding_function=cached,
                         persist_directory=persist_directory,
                         collection_metadata={'hnsw:space': 'cosine'})

    # 기존 데이터 비우기
    vectorstore.reset_collection()

    # 문서 추가
    vectorstore.add_documents(docs, ids=[d.metadata['chunk_id'] for d in docs])
    timing['embedding'] = time.time() - step

    # 사전 생성 그래프 (4장) - 청크 해시가 맞을 때만 사용, 아니면 기본 RAG로 동작
    report('검색기와 그래프를 준비하고 있어요')
    step = time.time()
    graph, graph_info = None, {'status': 'off'}
    if graph_path or use_neo4j:
        graph, graph_info = load_graph(graph_path, docs, min_coverage=min_graph_coverage,
                                       use_neo4j=use_neo4j, neo4j_search=neo4j_search)

    # 검색기 구성 (벡터 + BM25), 그래프는 체인을 만들 때 붙인다
    retriever = HybridRetriever(docs, vectorstore, k=k, use_bm25=use_bm25, graph_weight=graph_weight)
    timing['retriever'] = time.time() - step
    timing['total'] = time.time() - start
    return RagIndex(docs=docs, pages=len(pages), vectorstore=vectorstore, retriever=retriever, k=k,
                    graph=graph, graph_info=graph_info, new_embeddings=getattr(cached, 'last_new', None),
                    timing=timing, source=os.path.basename(filepath))


def make_llm(chat_model=None):
    from langchain_openai import ChatOpenAI
    # gpt-6-luna는 temperature 기본값(1)만 허용한다. 다른 모델에서 바꾸려면 OPENAI_TEMPERATURE를 지정한다.
    llm_kwargs = {'model': chat_model or os.getenv('OPENAI_CHAT_MODEL', CHAT_MODEL)}
    if os.getenv('OPENAI_TEMPERATURE'):
        llm_kwargs['temperature'] = float(os.environ['OPENAI_TEMPERATURE'])
    return ChatOpenAI(**llm_kwargs)


def make_chain(index, *, llm=None, chat_model=None, structured=False, with_sources=False,
               system_prompt=None, use_graph=True, agentic=False):
    """색인으로 RAG 체인을 만든다: LLM 선언 → 프롬프트 → 체인 (기본 / 구조화 / Agentic)."""
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.runnables import RunnableLambda, RunnablePassthrough

    retriever = index.retriever.with_graph(index.graph if use_graph else None)

    # LLM 선언
    llm = llm or make_llm(chat_model)

    # 프롬프트 구성
    system = system_prompt or SYSTEM_PROMPT
    prompt = ChatPromptTemplate.from_messages([('system', system), ('human', USER_TEMPLATE)])

    # RAG 체인 구성
    def prepare(x):
        if isinstance(x, str):
            x = {'input': x}
        question = x['input']
        history = x.get('history') or []
        previous = [m['content'] for m in history if m.get('role') == 'user'][-1:]
        start = time.time()
        emit_progress('start', '근거 찾기')
        found = retriever.invoke('\n'.join(previous + [question]))   # 후속 질문은 직전 질문과 함께 검색
        trace = dict(retriever.last_trace, timing={'retrieve': round(time.time() - start, 2)})
        emit_progress('end', '근거 찾기', sec=trace['timing']['retrieve'], found=len(found))
        return {'input': question, 'history': format_history(history),
                'docs': found, 'context': format_docs(found), 'trace': trace}

    if structured:
        generate = make_structured_step(llm, system)   # 3장에서 정의
    else:
        def generate(x):
            return {'answer': str((prompt | llm | StrOutputParser()).invoke(x)), 'issues': [], 'intent': None}

    def timed_generate(x):
        start = time.time()
        if not agentic:
            emit_progress('start', '답변 쓰기')
        result = generate(x)
        x.setdefault('trace', {}).setdefault('timing', {})['generate'] = round(time.time() - start, 2)
        if not agentic:
            emit_progress('end', '답변 쓰기', sec=x['trace']['timing']['generate'])
        return result

    answer_step = RunnableLambda(timed_generate)

    def finish(x):
        sources = [{**d.metadata, 'text': d.page_content.split('\n', 1)[-1][:600]} for d in x['docs']]
        result = {**x['result'], 'sources': sources, 'trace': x['trace']}
        return result if with_sources else result['answer']

    if agentic:
        return build_agentic_chain(llm, retriever, answer_step, finish, k=index.k)   # 5장에서 정의
    return (RunnableLambda(prepare)
            | RunnablePassthrough.assign(result=answer_step)
            | RunnableLambda(finish))


def rag_pipeline(filepath, *, chunk_tokens=CHUNK_TOKENS, overlap_tokens=OVERLAP_TOKENS, k=TOP_K, use_bm25=True,
                 structured=False, with_sources=False, persist_directory='chroma_db',
                 cache_dir='.embed_cache', embeddings=None, llm=None,
                 chat_model=None, embedding_model=None, system_prompt=None,
                 graph_path=None, min_graph_coverage=MIN_GRAPH_COVERAGE, use_neo4j=True, neo4j_search=True,
                 graph_weight=GRAPH_WEIGHT, agentic=False):
    """PDF 경로를 받아 문서 읽기부터 RAG 체인 구성까지 일괄 처리하고 RAG 체인을 반환한다.

    rag_chain = rag_pipeline(path + "AIVLE School 백서.pdf")
    rag_chain.invoke({"input": "질문"})                     # → 답변 문자열
    rag_chain.invoke({"input": "질문", "history": [...]})   # 이전 대화 반영
    graph_path를 주면 4장의 사전 생성 그래프를 청크 해시가 맞을 때만 검색에 함께 쓴다.
    agentic=True면 5장의 LangGraph 흐름(분류 → 검색 → 근거 평가 → 재검색 최대 1회 → 생성)으로 답한다.
    """
    # 1) 색인 만들기: PDF 읽기 → 분할 → 임베딩 → 벡터저장소 → 비우기 → 문서 추가 → 검색기
    index = build_index(filepath, chunk_tokens=chunk_tokens, overlap_tokens=overlap_tokens, k=k,
                        use_bm25=use_bm25, persist_directory=persist_directory, cache_dir=cache_dir,
                        embeddings=embeddings, embedding_model=embedding_model,
                        graph_path=graph_path, min_graph_coverage=min_graph_coverage,
                        use_neo4j=use_neo4j and bool(graph_path), neo4j_search=neo4j_search,
                        graph_weight=graph_weight)
    # 2) 체인 만들기: LLM 선언 → 프롬프트 → RAG 체인
    rag_chain = make_chain(index, llm=llm, chat_model=chat_model, structured=structured,
                           with_sources=with_sources, system_prompt=system_prompt,
                           use_graph=True, agentic=agentic)
    rag_pipeline.last_info = {'chunks': len(index.docs), 'pages': index.pages,
                              'new_embeddings': index.new_embeddings, 'documents': index.docs,
                              'graph': index.graph_info, 'index': index,
                              'retriever': index.retriever.with_graph(index.graph),
                              'agent_app': getattr(build_agentic_chain, 'last_app', None) if agentic else None}
    return rag_chain

# ==========================================================
# 3. JSON 구조화 답변과 인용 검증 (차단하지 않고 보정)
# ==========================================================
from typing import List, Literal

from pydantic import BaseModel, Field

from aivle_config import STRUCTURED_RULES   # 사람이 정하는 출력 규칙


class Fact(BaseModel):
    text: str = Field(description='백서 근거로 말하는 사실 문장 하나')
    citations: List[str] = Field(description='이 문장의 근거 청크 ID 목록. 예: ["c-1a2b3c4d5e6f"]')


class StructuredAnswer(BaseModel):
    intent: Literal['fact', 'experience', 'advice', 'out_of_scope', 'chat'] = Field(
        description='fact=규정·일정 등 사실, experience=선배 경험, advice=공부법·마음가짐 조언, '
                    'out_of_scope=백서에 없는 내용, chat=인사·잡담')
    facts: List[Fact] = Field(description='백서에서 확인한 사실 문장들. 각 문장에 근거 청크 ID 필수')
    advice: List[str] = Field(description='일반적인 조언 문장들. 인용 불필요')
    closing: str = Field(description='질문자를 격려하는 마무리 한두 문장')
    outside_knowledge: bool = Field(description='백서 밖 일반 지식을 사용했으면 true')


def page_label(meta):
    if meta['page_end'] != meta['page_start']:
        return f"p.{meta['page_start']}-{meta['page_end']}"
    return f"p.{meta['page_start']}"


def validate_answer(answer, docs):
    """인용 ID가 이번 검색 결과 안에 있는지 확인한다. 틀린 인용은 지우고 표시만 남긴다."""
    by_id = {d.metadata['chunk_id']: d.metadata for d in docs}
    facts, issues, cited = [], [], []
    for fact in answer.facts:
        valid = [c for c in fact.citations if c in by_id]
        invalid = [c for c in fact.citations if c not in by_id]
        if invalid:
            issues.append({'type': 'unknown_citation', 'ids': invalid, 'text': fact.text[:80]})
        if not fact.citations:
            issues.append({'type': 'missing_citation', 'text': fact.text[:80]})
        pages = []
        for cid in valid:
            label = page_label(by_id[cid])
            if label not in pages:
                pages.append(label)
            if cid not in cited:
                cited.append(cid)
        facts.append({'text': fact.text.strip(), 'citations': valid, 'pages': pages,
                      'unverified': not valid})
    return {'intent': answer.intent, 'facts': facts, 'advice': [a.strip() for a in answer.advice if a.strip()],
            'closing': answer.closing.strip(), 'outside_knowledge': answer.outside_knowledge,
            'issues': issues, 'cited_ids': cited}


def render_answer(checked):
    """검증 결과를 자연스러운 문단으로 이어 붙인다: 사실 → 조언 → 격려."""
    fact_lines = []
    for fact in checked['facts']:
        mark = f" ({', '.join(fact['pages'])})" if fact['pages'] else ' (근거 확인 필요)'
        sentence = fact['text']
        if sentence.endswith('.'):
            fact_lines.append(sentence[:-1] + mark + '.')
        else:
            fact_lines.append(sentence + mark)
    paragraphs = [' '.join(fact_lines), ' '.join(checked['advice']), checked['closing']]
    return '\n\n'.join(p for p in paragraphs if p)


def make_structured_step(llm, system):
    """구조화 출력 → 형식 오류 시 1회 재시도 → 그래도 실패하면 일반 답변으로 대체."""
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate

    structured_prompt = ChatPromptTemplate.from_messages(
        [('system', system + STRUCTURED_RULES), ('human', USER_TEMPLATE + '{format_error}')])
    plain_prompt = ChatPromptTemplate.from_messages([('system', system), ('human', USER_TEMPLATE)])
    structured_llm = llm.with_structured_output(StructuredAnswer)

    def step(x):
        error = ''
        for attempt in range(2):
            try:
                answer = (structured_prompt | structured_llm).invoke({**x, 'format_error': error})
                if answer is None:
                    raise ValueError('빈 구조화 응답')
                checked = validate_answer(answer, x['docs'])
                if attempt:
                    checked['issues'].append({'type': 'format_retry'})
                checked['answer'] = render_answer(checked)
                return checked
            except Exception as exc:     # 형식 오류: 오류 내용을 알려 주고 한 번 더 시도
                error = f'\n\n[직전 출력 형식 오류] {type(exc).__name__}: {str(exc)[:200]} — 지정한 JSON 형식을 지켜 다시 답하라.'
        text = (plain_prompt | llm | StrOutputParser()).invoke(x)
        return {'answer': str(text), 'intent': None, 'facts': [], 'advice': [], 'closing': '',
                'issues': [{'type': 'format_fallback'}], 'cited_ids': []}
    return step
# ==========================================================
# 4. 고도화: 그래프 (gpt-6-luna 사전 추출 + Neo4j)
#   - 청크마다 Luna를 한 번 호출해 문맥·예상 질문·개념·관계를 뽑아 JSON 캐시에 저장한다.
#   - 캐시 키는 청크 내용 해시다. 같은 백서면 그대로 재사용하고, 다른 PDF면 해시가 맞지 않아 그래프를 끈다.
#   - 그래프는 답변 근거가 아니라 "원문 청크를 찾는 검색 보조"다. 답변에는 항상 원문 청크가 들어간다.
#   - Neo4j는 NEO4J_URI가 있을 때만 쓰고, 조회는 읽기 전용 Cypher 템플릿으로만 한다.
# ==========================================================
import math
import unicodedata
from collections import Counter, defaultdict
from typing import List, Literal

from pydantic import BaseModel, Field

from aivle_config import CANONICAL, GRAPH_MODEL, GRAPH_PROMPT_VERSION, GRAPH_STOP, GRAPH_SYSTEM   # 사람이 정하는 지시문·용어

CONCEPT_TYPE = Literal['트랙', '과정', '프로그램', '규정', '혜택', '기관', '직무', '역량', '일정', '기타']
RELATION_TYPE = Literal['포함', '선행', '대상', '제공', '요구', '연결', '비교', '관련']


class Concept(BaseModel):
    name: str = Field(description="백서에 나온 표기 그대로의 짧은 명사구. 예: 'DX 트랙', '빅프로젝트', '훈련장려금'")
    type: CONCEPT_TYPE


class Relation(BaseModel):
    source: str = Field(description='concepts에 있는 개념 이름')
    relation: RELATION_TYPE
    target: str = Field(description='concepts에 있는 개념 이름')


class ChunkGraph(BaseModel):
    context: str = Field(description='이 청크가 백서에서 무엇을 다루는지 1~2문장 요약')
    questions: List[str] = Field(description='이 청크로 답할 수 있는 예비·현재 교육생의 질문 2~3개')
    concepts: List[Concept] = Field(description='핵심 개념 최대 8개')
    relations: List[Relation] = Field(description='청크에 명시된 개념 간 관계 최대 8개')


GRAPH_SKIP_PARTS = ('문서 안내',)   # 표지·백서 소개 청크는 그래프에서 뺀다 (검색에는 남김)
GRAPH_STOP_RE = re.compile(r'^(\d+기|부록[a-c]?\d*|\d{4}년(\d+월)?|\d+월)$')


def concept_key(name):
    """비교용 개념 키: 유니코드 정규화 + 공백 제거 + 소문자."""
    name = unicodedata.normalize('NFC', str(name)).strip().strip('\'"“”‘’')
    return re.sub(r'\s+', '', name).lower()


_ALIAS = {}
for _canon, _aliases in CANONICAL.items():
    for _name in [_canon] + _aliases:
        _ALIAS[re.sub(r'\s+', '', unicodedata.normalize('NFC', _name)).lower()] = _canon


def canonical(name):
    """표준 이름이 있으면 (표준 이름, 키), 없으면 (원래 이름, 키)."""
    key = concept_key(name)
    if key in _ALIAS:
        name = _ALIAS[key]
        key = concept_key(name)
    return re.sub(r'\s+', ' ', str(name)).strip(), key


def is_noise_concept(name, key, ctype):
    if len(key) < 2 or key in GRAPH_STOP or GRAPH_STOP_RE.match(key):
        return True
    if key in {concept_key(c) for c in CANONICAL}:
        return False
    return ctype == '기타' and (len(key) > 12 or '의 ' in name)   # 문장 조각


def clean_extraction(raw):
    """모델 출력 정리: 개념 중복·불용어 제거, 양 끝이 개념 목록에 없는 관계와 자기 자신 관계 제거."""
    concepts, seen = [], set()
    for c in raw.get('concepts', [])[:8]:
        name, key = canonical(c['name'])
        if key in seen or is_noise_concept(name, key, c['type']):
            continue
        seen.add(key)
        concepts.append({'name': name, 'type': c['type'], 'key': key})
    relations = []
    for r in raw.get('relations', [])[:8]:
        a, b = canonical(r['source'])[1], canonical(r['target'])[1]
        if a in seen and b in seen and a != b:
            relations.append({'source': a, 'relation': r['relation'], 'target': b})
    return {'context': str(raw.get('context', '')).strip(),
            'questions': [q.strip() for q in raw.get('questions', [])[:3] if q.strip()],
            'concepts': concepts, 'relations': relations}


def _save_json(obj, path):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def extract_graph(docs, cache_path, llm=None, batch_size=20, max_concurrency=8, verbose=True):
    """청크마다 Luna를 1회 호출해 그래프 정보를 뽑는다. 이미 뽑은 청크(같은 내용 해시)는 건너뛴다.

    실패한 청크는 저장하지 않으므로, 다시 실행하면 실패분만 다시 호출한다.
    """
    from langchain_core.prompts import ChatPromptTemplate
    if llm is None:
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(model=os.getenv('OPENAI_GRAPH_MODEL') or GRAPH_MODEL or os.getenv('OPENAI_CHAT_MODEL', CHAT_MODEL))
    cache = {'version': 1, 'prompt_version': GRAPH_PROMPT_VERSION, 'items': {}}
    if os.path.exists(cache_path):
        with open(cache_path, encoding='utf-8') as f:
            old = json.load(f)
        if old.get('prompt_version', 1) == GRAPH_PROMPT_VERSION:
            cache = old
        else:                                   # 지시문이 바뀌었으면 예전 캐시는 따로 두고 새로 뽑는다
            os.replace(cache_path, cache_path.replace('.json', f".v{old.get('prompt_version', 1)}.json"))
            if verbose:
                print(f"  지시문 버전이 바뀌어 그래프를 새로 뽑습니다 (v{old.get('prompt_version', 1)} → v{GRAPH_PROMPT_VERSION})")
    items = cache['items']
    eligible = [d for d in docs if d.metadata['part'] not in GRAPH_SKIP_PARTS]
    todo = [d for d in eligible if d.metadata['content_hash'] not in items]
    prompt = ChatPromptTemplate.from_messages([('system', GRAPH_SYSTEM), ('human', '[청크]\n{chunk}')])
    chain = prompt | llm.with_structured_output(ChunkGraph)
    failed = 0
    for start in range(0, len(todo), batch_size):
        batch = todo[start:start + batch_size]
        outputs = chain.batch([{'chunk': d.page_content} for d in batch],
                              config={'max_concurrency': max_concurrency}, return_exceptions=True)
        for doc, out in zip(batch, outputs):
            if isinstance(out, Exception):
                failed += 1
                continue
            raw = out.model_dump() if hasattr(out, 'model_dump') else dict(out)
            items[doc.metadata['content_hash']] = clean_extraction(raw)
        _save_json(cache, cache_path)
        if verbose:
            print(f'  그래프 추출 {min(start + batch_size, len(todo))}/{len(todo)} (실패 {failed})')
    extract_graph.last_info = {'new': len(todo) - failed, 'failed': failed,
                               'reused': len(eligible) - len(todo), 'eligible': len(eligible),
                               'skipped(문서 안내)': len(docs) - len(eligible)}
    return cache


def document_hash(docs):
    """문서 전체 해시 = 청크 내용 해시들을 정렬해 이어 붙인 해시."""
    joined = '\n'.join(sorted(d.metadata['content_hash'] for d in docs))
    return hashlib.sha256(joined.encode('utf-8')).hexdigest()


class Neo4jStore:
    """Neo4j 저장·조회. 앱에서는 읽기 전용 템플릿만 사용하고, 쓰기는 노트북에서 그래프를 올릴 때만 한다."""

    # 읽기 전용 Cypher 템플릿 (질문 문자열을 쿼리에 넣지 않고 파라미터로만 전달)
    CYPHER_HAS_DOC = 'MATCH (d:Document {doc_hash: $doc_hash}) RETURN count(d) AS n'
    CYPHER_DIRECT = """
        MATCH (c:Concept {doc_hash: $doc_hash}) WHERE c.key IN $keys
        MATCH (c)<-[:MENTIONS]-(k:Chunk)
        RETURN k.chunk_id AS chunk_id, c.key AS key, c.df AS df, 1.0 AS hop_weight"""
    CYPHER_NEIGHBOR = """
        MATCH (c:Concept {doc_hash: $doc_hash}) WHERE c.key IN $keys
        MATCH (c)-[r:RELATED]-(n:Concept)<-[:MENTIONS]-(k:Chunk)
        WHERE NOT n.key IN $keys AND r.type <> '관련'
        WITH DISTINCT c, n, k
        RETURN k.chunk_id AS chunk_id, n.key AS key, n.df AS df, 0.3 AS hop_weight"""
    CYPHER_STATS = """
        MATCH (d:Document {doc_hash: $doc_hash})
        RETURN d.chunks AS chunks,
               COUNT { MATCH (c:Concept {doc_hash: $doc_hash}) } AS concepts,
               COUNT { MATCH (:Concept {doc_hash: $doc_hash})-[r:RELATED]->() } AS relations"""

    CYPHER_ITEMS = """
        MATCH (d:Document)-[:HAS_CHUNK]->(k:Chunk) WHERE k.content_hash IN $hashes
        OPTIONAL MATCH (k)-[:MENTIONS]->(c:Concept)
        RETURN d.doc_hash AS doc_hash, k.content_hash AS content_hash, k.chunk_id AS chunk_id,
               k.context AS context, k.questions AS questions,
               collect(c {.name, .type, .key}) AS concepts"""
    CYPHER_ITEM_RELATIONS = """
        MATCH (a:Concept {doc_hash: $doc_hash})-[r:RELATED]->(b:Concept)
        RETURN a.key AS source, r.type AS relation, b.key AS target, r.chunk_ids AS chunk_ids"""

    def __init__(self, uri, user, password, database=None):
        from neo4j import GraphDatabase
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.driver.verify_connectivity()
        self.database = database

    @classmethod
    def from_env(cls):
        uri = os.getenv('NEO4J_URI')
        if not uri:
            return None
        return cls(uri, os.getenv('NEO4J_USERNAME', 'neo4j'), os.getenv('NEO4J_PASSWORD', ''),
                   os.getenv('NEO4J_DATABASE') or None)

    def _read(self, query, **params):
        with self.driver.session(database=self.database, default_access_mode='READ') as session:
            return session.execute_read(lambda tx: [r.data() for r in tx.run(query, **params)])

    def has_document(self, doc_hash):
        return self._read(self.CYPHER_HAS_DOC, doc_hash=doc_hash)[0]['n'] > 0

    def related_rows(self, doc_hash, keys):
        return (self._read(self.CYPHER_DIRECT, doc_hash=doc_hash, keys=keys)
                + self._read(self.CYPHER_NEIGHBOR, doc_hash=doc_hash, keys=keys))

    def load_items(self, content_hashes):
        """앱용: 청크 해시로 사전 생성 그래프를 Neo4j에서 읽어 JSON 캐시와 같은 모양으로 돌려준다."""
        rows = self._read(self.CYPHER_ITEMS, hashes=list(content_hashes))
        if not rows:
            return {}, None
        doc_hash = Counter(r['doc_hash'] for r in rows).most_common(1)[0][0]
        rows = [r for r in rows if r['doc_hash'] == doc_hash]
        by_chunk = defaultdict(list)
        for r in self._read(self.CYPHER_ITEM_RELATIONS, doc_hash=doc_hash):
            for cid in r['chunk_ids'] or []:
                by_chunk[cid].append({'source': r['source'], 'relation': r['relation'], 'target': r['target']})
        items = {r['content_hash']: {'context': r['context'] or '', 'questions': r['questions'] or [],
                                     'concepts': [c for c in r['concepts'] if c],
                                     'relations': by_chunk.get(r['chunk_id'], [])} for r in rows}
        return items, doc_hash

    def stats(self, doc_hash):
        rows = self._read(self.CYPHER_STATS, doc_hash=doc_hash)
        return rows[0] if rows else {}

    def write_graph(self, doc_hash, docs, items, source=''):
        """노트북 전용: 그래프를 Neo4j에 올린다 (같은 문서 해시는 지우고 다시 쓴다)."""
        tag = doc_hash[:12]
        chunks, mentions, rel_map, concepts = [], [], {}, {}
        for d in docs:
            m, item = d.metadata, items.get(d.metadata['content_hash'])
            if not item:
                continue
            chunks.append({'uid': f"{tag}:{m['chunk_id']}", 'chunk_id': m['chunk_id'], 'title': m['title'],
                           'part': m['part'], 'page_start': m['page_start'],
                           'content_hash': m['content_hash'], 'context': item['context'],
                           'questions': item['questions']})
            for c in item['concepts']:
                info = concepts.setdefault(c['key'], {'uid': f"{tag}:{c['key']}", 'key': c['key'],
                                                      'name': c['name'], 'type': c['type'], 'chunks': set()})
                info['chunks'].add(m['chunk_id'])
                mentions.append({'chunk': f"{tag}:{m['chunk_id']}", 'concept': f"{tag}:{c['key']}"})
            for r in item['relations']:
                key = (r['source'], r['relation'], r['target'])
                rel_map.setdefault(key, set()).add(m['chunk_id'])
        concept_rows = [{**{k: v for k, v in c.items() if k != 'chunks'}, 'df': len(c['chunks'])}
                        for c in concepts.values()]
        relations = [{'a': f'{tag}:{a}', 'b': f'{tag}:{b}', 'type': t, 'chunk_ids': sorted(ids)}
                     for (a, t, b), ids in rel_map.items()]
        statements = [
            ('CREATE CONSTRAINT chunk_uid IF NOT EXISTS FOR (k:Chunk) REQUIRE k.uid IS UNIQUE', {}),
            ('CREATE CONSTRAINT concept_uid IF NOT EXISTS FOR (c:Concept) REQUIRE c.uid IS UNIQUE', {}),
            ('CREATE INDEX chunk_hash IF NOT EXISTS FOR (k:Chunk) ON (k.content_hash)', {}),
            ('MATCH (n) WHERE n.doc_hash = $doc_hash DETACH DELETE n', {'doc_hash': doc_hash}),
            ('CREATE (:Document {doc_hash: $doc_hash, name: $source, source: $source, chunks: $n})',
             {'doc_hash': doc_hash, 'source': source, 'n': len(chunks)}),
            ('''UNWIND $rows AS r MATCH (d:Document {doc_hash: $doc_hash})
                CREATE (k:Chunk {uid: r.uid, doc_hash: $doc_hash, chunk_id: r.chunk_id, name: r.title, title: r.title,
                                 part: r.part, page_start: r.page_start, content_hash: r.content_hash,
                                 context: r.context, questions: r.questions})
                CREATE (d)-[:HAS_CHUNK]->(k)''', {'rows': chunks, 'doc_hash': doc_hash}),
            ('''UNWIND $rows AS r
                CREATE (:Concept {uid: r.uid, doc_hash: $doc_hash, key: r.key, name: r.name,
                                  type: r.type, df: r.df})''', {'rows': concept_rows, 'doc_hash': doc_hash}),
            ('''UNWIND $rows AS r MATCH (k:Chunk {uid: r.chunk}) MATCH (c:Concept {uid: r.concept})
                MERGE (k)-[:MENTIONS]->(c)''', {'rows': mentions}),
            ('''UNWIND $rows AS r MATCH (a:Concept {uid: r.a}) MATCH (b:Concept {uid: r.b})
                CREATE (a)-[:RELATED {type: r.type, chunk_ids: r.chunk_ids}]->(b)''', {'rows': relations}),
        ]
        with self.driver.session(database=self.database) as session:
            for query, params in statements:
                session.run(query, **params).consume()
        return {'chunks': len(chunks), 'concepts': len(concept_rows), 'relations': len(relations)}

    def close(self):
        self.driver.close()


class GraphIndex:
    """질문에서 개념을 찾고, 그 개념(1홉 이웃 포함)과 예상 질문으로 원문 청크 순위를 만든다."""

    def __init__(self, docs, items, doc_hash, store=None):
        self.doc_hash, self.store = doc_hash, store
        self.n_chunks = len(docs)
        self.concepts = {}                 # key -> {'name','type','chunks'}
        self.neighbors = defaultdict(lambda: defaultdict(set))  # key -> 이웃 key -> 관계 종류
        q_texts, self.q_chunks = [], []
        self.chunk_questions = {}                # 청크 ID -> 미리 만든 예상 질문 (후속 질문 추천용)
        for d in docs:
            item = items.get(d.metadata['content_hash'])
            if not item:
                continue
            cid = d.metadata['chunk_id']
            for c in item['concepts']:
                info = self.concepts.setdefault(c['key'], {'name': c['name'], 'type': c['type'], 'chunks': set()})
                info['chunks'].add(cid)
            for r in item['relations']:
                self.neighbors[r['source']][r['target']].add(r['relation'])
                self.neighbors[r['target']][r['source']].add(r['relation'])
            for q in item['questions']:
                q_texts.append(q)
                self.q_chunks.append(cid)
            self.chunk_questions[cid] = list(item['questions'])
        # 질문 속 표기 → 개념 키 (개념 이름 + 표준 이름의 다른 표기)
        self.surface = {k: k for k in self.concepts}
        for alias_key, canon in _ALIAS.items():
            canon_key = concept_key(canon)
            if canon_key in self.concepts:
                self.surface.setdefault(alias_key, canon_key)
        self.surface_by_len = sorted(self.surface, key=len, reverse=True)
        self.tokenize = korean_tokenizer()
        self.q_bm25 = None
        if q_texts:
            from rank_bm25 import BM25Okapi
            self.q_bm25 = BM25Okapi([self.tokenize(q) or ['_'] for q in q_texts])
        self.last_trace = {}

    def match_concepts(self, query, max_concepts=6):
        """질문 문자열 안에 들어 있는 개념 이름(다른 표기 포함)을 긴 것부터 찾는다 (공백 무시)."""
        q = concept_key(query)
        found, spans = [], []
        for surface in self.surface_by_len:
            if surface in q and not any(surface in s for s in spans):
                spans.append(surface)
                key = self.surface[surface]
                if key not in found:
                    found.append(key)
            if len(found) >= max_concepts:
                break
        return found

    def specific_neighbors(self, key):
        """'관련'이 아닌 구체적인 관계로 이어진 이웃만 돌려준다."""
        return [nb for nb, types in self.neighbors.get(key, {}).items() if types - {'관련'}]

    def _rows_local(self, keys):
        rows = []
        for key in keys:
            info = self.concepts[key]
            rows += [{'chunk_id': c, 'key': key, 'df': len(info['chunks']), 'hop_weight': 1.0} for c in info['chunks']]
            for nb in self.specific_neighbors(key):
                if nb in keys:
                    continue
                nb_info = self.concepts[nb]
                rows += [{'chunk_id': c, 'key': nb, 'df': len(nb_info['chunks']), 'hop_weight': 0.3}
                         for c in nb_info['chunks']]
        return rows

    def search(self, query, limit=10):
        keys = self.match_concepts(query)
        scores = defaultdict(float)
        backend = 'local'
        if keys:
            rows = None
            if self.store is not None:
                try:
                    rows, backend = self.store.related_rows(self.doc_hash, keys), 'neo4j'
                except Exception:
                    rows = None                      # Neo4j 오류 시 같은 데이터의 로컬 그래프로 대체
            if rows is None:
                rows = self._rows_local(keys)
            for r in rows:                           # 흔한 개념일수록 낮게 (IDF)
                idf = math.log(1 + self.n_chunks / max(r['df'] or 1, 1))
                scores[r['chunk_id']] += r['hop_weight'] * idf
        if self.q_bm25 is not None:                  # 사전 생성한 예상 질문과도 맞춰 본다
            q_scores = self.q_bm25.get_scores(self.tokenize(query) or ['_'])
            order = sorted(range(len(q_scores)), key=lambda i: -q_scores[i])[:limit]
            top = max(scores.values(), default=1.0) or 1.0
            for rank, i in enumerate(order):
                if q_scores[i] <= 0:
                    break
                scores[self.q_chunks[i]] += top * 0.5 / (rank + 1)
        ranked = sorted(scores, key=lambda c: -scores[c])[:limit]
        self.last_trace = {'concepts': [self.concepts[k]['name'] for k in keys], 'keys': keys,
                           'backend': backend, 'chunks': ranked}
        return ranked


def _graph_questions_for(self, chunk_ids, limit=6):
    """인용된 청크의 예상 질문을 순서대로 모은다 (중복 제거)."""
    seen, out = set(), []
    for cid in chunk_ids:
        for q in self.chunk_questions.get(cid, []):
            if q not in seen:
                seen.add(q)
                out.append(q)
    return out[:limit]


def _graph_subgraph(self, keys, max_nodes=10, per_concept=3):
    """질문 개념과, 구체적인 관계로 이어진 이웃(개념마다 최대 3개)으로 작은 개념 지도를 만든다."""
    keys = [k for k in keys if k in self.concepts][:3]
    chosen, edges, seen = list(keys), [], set()
    for key in keys:
        neighbors = sorted(self.specific_neighbors(key), key=lambda n: -len(self.concepts[n]['chunks']))
        for nb in neighbors[:per_concept]:
            if len(chosen) >= max_nodes and nb not in chosen:
                break
            if nb not in chosen:
                chosen.append(nb)
    for key in keys:                                   # 간선은 질문 개념에 닿는 것만 그린다
        for nb, types in self.neighbors.get(key, {}).items():
            pair = tuple(sorted((key, nb)))
            specific = sorted(types - {'관련'})
            if nb in chosen and pair not in seen and specific:
                seen.add(pair)
                edges.append({'source': key, 'target': nb, 'types': specific})
    nodes = [{'key': k, 'name': self.concepts[k]['name'], 'type': self.concepts[k]['type'], 'query': k in keys}
             for k in chosen]
    return {'nodes': nodes, 'edges': edges}


GraphIndex.questions_for = _graph_questions_for
GraphIndex.subgraph = _graph_subgraph


def load_graph(graph_path, docs, min_coverage=0.8, use_neo4j=True, neo4j_search=True):
    """사전 생성 그래프를 불러온다. 청크 해시가 min_coverage 이상 맞을 때만 GraphIndex를 돌려준다.

    - graph_path(JSON)가 있으면 JSON에서, 없으면 Neo4j(NEO4J_URI)에서 청크 해시로 읽는다.
    - neo4j_search=True면 질문마다 Neo4j를 조회하고, False면 읽어 온 그래프로 메모리에서 찾는다(더 빠름).
    """
    eligible = [d for d in docs if d.metadata['part'] not in GRAPH_SKIP_PARTS]
    hashes = [d.metadata['content_hash'] for d in eligible]
    items, source, store, stored_hash = {}, None, None, None
    if graph_path and os.path.exists(graph_path):
        with open(graph_path, encoding='utf-8') as f:
            items, source = json.load(f).get('items', {}), 'json'
    if use_neo4j and os.getenv('NEO4J_URI'):
        try:
            store = Neo4jStore.from_env()
            if source is None:
                items, stored_hash = store.load_items(hashes)
                source = 'neo4j'
        except Exception as e:
            store = None
            if source is None:
                return None, {'status': 'error', 'reason': f'Neo4j 연결 실패: {type(e).__name__}'}
    if source is None:
        return None, {'status': 'no_graph'}
    matched = sum(1 for h in hashes if h in items)
    coverage = matched / max(len(eligible), 1)
    info = {'status': 'hash_mismatch', 'source': source, 'coverage': round(coverage, 3), 'matched': matched}
    if coverage < min_coverage:
        if store is not None:
            store.close()
        return None, info                            # 다른 PDF: 그래프 없이 기본 RAG
    doc_hash = stored_hash or document_hash(docs)
    backend = 'local'
    if store is not None and neo4j_search:
        try:
            if store.has_document(doc_hash):
                backend = 'neo4j'
            else:
                store.close()
                store, backend = None, 'local (Neo4j에 이 문서 그래프 없음)'
        except Exception as e:
            store, backend = None, f'local (Neo4j 연결 실패: {type(e).__name__})'
    elif store is not None:
        store.close()
        store = None
    graph = GraphIndex(docs, items, doc_hash, store)
    info.update({'status': 'on', 'backend': backend, 'doc_hash': doc_hash[:12],
                 'concepts': len(graph.concepts),
                 'concept_pairs': sum(len(v) for v in graph.neighbors.values()) // 2,
                 'specific_pairs': sum(len(graph.specific_neighbors(k)) for k in graph.concepts) // 2})
    return graph, info
# ==========================================================
# 5. 고도화: Agentic (LangGraph)
#   분류 → 검색 → 근거 평가 → (부족하면) 재검색 최대 1회 → 생성
#   - 분류: 인사·잡담은 검색을 건너뛰고, 후속 질문은 생략된 대상을 채운 검색 질문으로 바꾼다.
#   - 근거 평가: 검색 근거로 답할 수 있는지 판단하고, 부족하면 다른 표현의 검색 질문을 만든다.
#   - 재검색은 한 번만 하고 다시 평가하지 않는다 (호출 수 상한).
#   - 분류·평가 호출이 실패하면 기본 경로(검색 → 생성)로 계속 진행한다.
#   - 생성은 2·3장의 답변 단계를 그대로 쓰므로 인용 검증도 그대로 적용된다.
# ==========================================================
from typing import TypedDict

from aivle_config import GRADE_SYSTEM, ROUTE_SYSTEM   # 사람이 정하는 지시문


class Route(BaseModel):
    route: Literal['chat', 'whitepaper'] = Field(
        description='chat=인사·잡담·도우미 소개 요청, whitepaper=에이블스쿨·백서·학습에 관한 질문')
    search_query: str = Field(description='백서 검색에 쓸 독립된 질문. 이전 대화에서 생략된 대상을 채워 쓴다')


class Grade(BaseModel):
    sufficient: bool = Field(description='검색 근거만으로 질문의 핵심에 답할 수 있으면 true')
    missing: str = Field(description='부족한 정보. 충분하면 빈 문자열')
    retry_query: str = Field(description='부족한 정보를 찾기 위한 다른 표현의 검색 질문. 충분하면 빈 문자열')


def brief_docs(docs, limit=350):
    """근거 평가용 요약: 청크 머리표 + 본문 앞부분."""
    lines = []
    for d in docs:
        m = d.metadata
        body = d.page_content.split('\n', 1)[-1].replace('\n', ' ')[:limit]
        lines.append(f"[{m['chunk_id']} | {m['part']} | {m['title']}] {body}")
    return '\n'.join(lines) or '(검색된 근거 없음)'


def merge_docs(first, second, limit):
    """두 검색 결과를 순서를 지키며 합치고 중복을 뺀다."""
    seen, merged = set(), []
    for d in list(first) + list(second):
        cid = d.metadata['chunk_id']
        if cid not in seen:
            seen.add(cid)
            merged.append(d)
    return merged[:limit]


class AgentState(TypedDict, total=False):
    input: str
    history_text: str
    route: str
    search_query: str
    docs: list
    trace: dict
    grade: dict
    retried: bool
    retry_query: str
    steps: list
    result: dict


def build_agentic_chain(llm, retriever, answer_step, finish, k=5, max_docs=None):
    """LangGraph로 Agentic 흐름을 만들고, 기존 체인과 같은 invoke 형식의 Runnable로 감싼다."""
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.runnables import RunnableLambda
    from langgraph.graph import END, START, StateGraph

    max_docs = max_docs or k + 3
    route_chain = ChatPromptTemplate.from_messages(
        [('system', ROUTE_SYSTEM), ('human', '[이전 대화]\n{history}\n\n[질문]\n{input}')]
    ) | llm.with_structured_output(Route)
    grade_chain = ChatPromptTemplate.from_messages(
        [('system', GRADE_SYSTEM), ('human', '[질문]\n{input}\n\n[검색 근거]\n{brief}')]
    ) | llm.with_structured_output(Grade)

    def classify(state):
        try:
            r = route_chain.invoke({'history': state['history_text'], 'input': state['input']})
            route, query, note = r.route, (r.search_query or state['input']).strip(), ''
        except Exception as exc:                      # 분류 실패 → 기본 경로
            route, query, note = 'whitepaper', state['input'], f'분류 실패({type(exc).__name__})'
        step = {'node': '분류', 'route': route, 'search_query': query}
        if note:
            step['note'] = note
        return {'route': route, 'search_query': query, 'steps': state['steps'] + [step]}

    def retrieve(state):
        docs = retriever.invoke(state['search_query'])
        return {'docs': docs, 'trace': dict(retriever.last_trace),
                'steps': state['steps'] + [{'node': '검색', 'query': state['search_query'], 'found': len(docs)}]}

    def grade(state):
        try:
            g = grade_chain.invoke({'input': state['input'], 'brief': brief_docs(state['docs'])})
            result = {'sufficient': bool(g.sufficient), 'missing': g.missing.strip(),
                      'retry_query': g.retry_query.strip()}
        except Exception as exc:                      # 평가 실패 → 그대로 생성
            result = {'sufficient': True, 'missing': '', 'retry_query': '', 'note': f'평가 실패({type(exc).__name__})'}
        if not result['sufficient'] and not result['retry_query']:
            result['retry_query'] = f"{state['search_query']} {result['missing']}".strip()
        return {'grade': result, 'steps': state['steps'] + [{'node': '근거 평가', **result}]}

    def re_retrieve(state):
        query = state['grade']['retry_query']
        more = retriever.invoke(query)
        merged = merge_docs(state['docs'], more, max_docs)
        trace = dict(state['trace'])
        trace['retry'] = dict(retriever.last_trace)
        return {'docs': merged, 'trace': trace, 'retried': True, 'retry_query': query,
                'steps': state['steps'] + [{'node': '재검색', 'query': query,
                                            'added': len(merged) - len(state['docs'])}]}

    def generate(state):
        docs = state.get('docs', [])
        x = {'input': state['input'], 'history': state['history_text'], 'docs': docs, 'context': format_docs(docs)}
        result = answer_step.invoke(x)
        return {'result': result, 'steps': state['steps'] + [{'node': '생성', 'docs': len(docs)}]}

    labels = {'classify': '질문 분석', 'retrieve': '근거 찾기', 'grade': '근거 평가',
              're_retrieve': '다시 찾기', 'generate': '답변 쓰기'}

    def timed(node, name):
        """각 단계의 소요 시간(초)을 단계 기록에 붙이고, 진행 상황을 알린다."""
        def wrapper(state):
            start = time.time()
            emit_progress('start', labels[name])
            out = node(state)
            out['steps'][-1]['sec'] = round(time.time() - start, 2)
            emit_progress('end', labels[name], **out['steps'][-1])
            return out
        return wrapper

    graph = StateGraph(AgentState)
    graph.add_node('classify', timed(classify, 'classify'))
    graph.add_node('retrieve', timed(retrieve, 'retrieve'))
    graph.add_node('grade', timed(grade, 'grade'))
    graph.add_node('re_retrieve', timed(re_retrieve, 're_retrieve'))
    graph.add_node('generate', timed(generate, 'generate'))
    graph.add_edge(START, 'classify')
    graph.add_conditional_edges('classify', lambda s: 'generate' if s['route'] == 'chat' else 'retrieve',
                                {'generate': 'generate', 'retrieve': 'retrieve'})
    graph.add_edge('retrieve', 'grade')
    graph.add_conditional_edges('grade', lambda s: 'generate' if s['grade']['sufficient'] else 're_retrieve',
                                {'generate': 'generate', 're_retrieve': 're_retrieve'})
    graph.add_edge('re_retrieve', 'generate')         # 재검색은 최대 1회: 다시 평가하지 않는다
    graph.add_edge('generate', END)
    app = graph.compile()
    build_agentic_chain.last_app = app

    def run(x):
        if isinstance(x, str):
            x = {'input': x}
        history = x.get('history') or []
        state = app.invoke({'input': x['input'], 'history_text': format_history(history), 'steps': []})
        trace = dict(state.get('trace') or {'vector': [], 'bm25': [], 'graph': [], 'graph_concepts': []})
        trace['timing'] = {s['node']: s.get('sec') for s in state['steps']}
        trace['agent'] = {'route': state.get('route'), 'search_query': state.get('search_query'),
                          'grade': state.get('grade'), 'retried': state.get('retried', False),
                          'retry_query': state.get('retry_query'), 'steps': state['steps']}
        return finish({'result': state['result'], 'docs': state.get('docs', []), 'trace': trace})

    return RunnableLambda(run)
