#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

import logging
import random
from collections import Counter

from common.token_utils import num_tokens_from_string
import re
import copy
import roman_numbers as r
from word2number import w2n
from cn2an import cn2an
from PIL import Image

import chardet

__all__ = ['rag_tokenizer']

all_codecs = [
    'utf-8', 'gb2312', 'gbk', 'utf_16', 'ascii', 'big5', 'big5hkscs',
    'cp037', 'cp273', 'cp424', 'cp437',
    'cp500', 'cp720', 'cp737', 'cp775', 'cp850', 'cp852', 'cp855', 'cp856', 'cp857',
    'cp858', 'cp860', 'cp861', 'cp862', 'cp863', 'cp864', 'cp865', 'cp866', 'cp869',
    'cp874', 'cp875', 'cp932', 'cp949', 'cp950', 'cp1006', 'cp1026', 'cp1125',
    'cp1140', 'cp1250', 'cp1251', 'cp1252', 'cp1253', 'cp1254', 'cp1255', 'cp1256',
    'cp1257', 'cp1258', 'euc_jp', 'euc_jis_2004', 'euc_jisx0213', 'euc_kr',
    'gb18030', 'hz', 'iso2022_jp', 'iso2022_jp_1', 'iso2022_jp_2',
    'iso2022_jp_2004', 'iso2022_jp_3', 'iso2022_jp_ext', 'iso2022_kr', 'latin_1',
    'iso8859_2', 'iso8859_3', 'iso8859_4', 'iso8859_5', 'iso8859_6', 'iso8859_7',
    'iso8859_8', 'iso8859_9', 'iso8859_10', 'iso8859_11', 'iso8859_13',
    'iso8859_14', 'iso8859_15', 'iso8859_16', 'johab', 'koi8_r', 'koi8_t', 'koi8_u',
    'kz1048', 'mac_cyrillic', 'mac_greek', 'mac_iceland', 'mac_latin2', 'mac_roman',
    'mac_turkish', 'ptcp154', 'shift_jis', 'shift_jis_2004', 'shift_jisx0213',
    'utf_32', 'utf_32_be', 'utf_32_le', 'utf_16_be', 'utf_16_le', 'utf_7', 'windows-1250', 'windows-1251',
    'windows-1252', 'windows-1253', 'windows-1254', 'windows-1255', 'windows-1256',
    'windows-1257', 'windows-1258', 'latin-2'
]


def find_codec(blob):
    detected = chardet.detect(blob[:1024])
    if detected['confidence'] > 0.5:
        if detected['encoding'] == "ascii":
            return "utf-8"

    for c in all_codecs:
        try:
            blob[:1024].decode(c)
            return c
        except Exception:
            pass
        try:
            blob.decode(c)
            return c
        except Exception:
            pass

    return "utf-8"


QUESTION_PATTERN = [
    r"第([零一二三四五六七八九十百0-9]+)问",
    r"第([零一二三四五六七八九十百0-9]+)条",
    r"[\(（]([零一二三四五六七八九十百]+)[\)）]",
    r"第([0-9]+)问",
    r"第([0-9]+)条",
    r"([0-9]{1,2})[\. 、]",
    r"([零一二三四五六七八九十百]+)[ 、]",
    r"[\(（]([0-9]{1,2})[\)）]",
    r"QUESTION (ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN)",
    r"QUESTION (I+V?|VI*|XI|IX|X)",
    r"QUESTION ([0-9]+)",
]


def has_qbullet(reg, box, last_box, last_index, last_bull, bull_x0_list):
    section, last_section = box['text'], last_box['text']
    q_reg = r'(\w|\W)*?(?:？|\?|\n|$)+'
    full_reg = reg + q_reg
    has_bull = re.match(full_reg, section)
    index_str = None
    if has_bull:
        if 'x0' not in last_box:
            last_box['x0'] = box['x0']
        if 'top' not in last_box:
            last_box['top'] = box['top']
        if last_bull and box['x0'] - last_box['x0'] > 10:
            return None, last_index
        if not last_bull and box['x0'] >= last_box['x0'] and box['top'] - last_box['top'] < 20:
            return None, last_index
        avg_bull_x0 = 0
        if bull_x0_list:
            avg_bull_x0 = sum(bull_x0_list) / len(bull_x0_list)
        else:
            avg_bull_x0 = box['x0']
        if box['x0'] - avg_bull_x0 > 10:
            return None, last_index
        index_str = has_bull.group(1)
        index = index_int(index_str)
        if last_section[-1] == ':' or last_section[-1] == '：':
            return None, last_index
        if not last_index or index >= last_index:
            bull_x0_list.append(box['x0'])
            return has_bull, index
        if section[-1] == '?' or section[-1] == '？':
            bull_x0_list.append(box['x0'])
            return has_bull, index
        if box['layout_type'] == 'title':
            bull_x0_list.append(box['x0'])
            return has_bull, index
        pure_section = section.lstrip(re.match(reg, section).group()).lower()
        ask_reg = r'(what|when|where|how|why|which|who|whose|为什么|为啥|哪)'
        if re.match(ask_reg, pure_section):
            bull_x0_list.append(box['x0'])
            return has_bull, index
    return None, last_index


def index_int(index_str):
    res = -1
    try:
        res = int(index_str)
    except ValueError:
        try:
            res = w2n.word_to_num(index_str)
        except ValueError:
            try:
                res = cn2an(index_str)
            except ValueError:
                try:
                    res = r.number(index_str)
                except ValueError:
                    return -1
    return res


def qbullets_category(sections):
    global QUESTION_PATTERN
    hits = [0] * len(QUESTION_PATTERN)
    for i, pro in enumerate(QUESTION_PATTERN):
        for sec in sections:
            if re.match(pro, sec) and not not_bullet(sec):
                hits[i] += 1
                break
    maximum = 0
    res = -1
    for i, h in enumerate(hits):
        if h <= maximum:
            continue
        res = i
        maximum = h
    return res, QUESTION_PATTERN[res]


BULLET_PATTERN = [[
    r"第[零一二三四五六七八九十百0-9]+(分?编|部分)",
    r"第[零一二三四五六七八九十百0-9]+章",
    r"第[零一二三四五六七八九十百0-9]+节",
    r"第[零一二三四五六七八九十百0-9]+条",
    r"[\(（][零一二三四五六七八九十百]+[\)）]",
], [
    r"第[0-9]+章",
    r"第[0-9]+节",
    r"[0-9]{,2}[\. 、]",
    r"[0-9]{,2}\.[0-9]{,2}[^a-zA-Z/%~-]",
    r"[0-9]{,2}\.[0-9]{,2}\.[0-9]{,2}",
    r"[0-9]{,2}\.[0-9]{,2}\.[0-9]{,2}\.[0-9]{,2}",
], [
    r"第[零一二三四五六七八九十百0-9]+章",
    r"第[零一二三四五六七八九十百0-9]+节",
    r"[零一二三四五六七八九十百]+[ 、]",
    r"[\(（][零一二三四五六七八九十百]+[\)）]",
    r"[\(（][0-9]{,2}[\)）]",
], [
    r"PART (ONE|TWO|THREE|FOUR|FIVE|SIX|SEVEN|EIGHT|NINE|TEN)",
    r"Chapter (I+V?|VI*|XI|IX|X)",
    r"Section [0-9]+",
    r"Article [0-9]+"
], [
    r"^#[^#]",
    r"^##[^#]",
    r"^###.*",
    r"^####.*",
    r"^#####.*",
    r"^######.*",
]
]


def random_choices(arr, k):
    k = min(len(arr), k)
    return random.choices(arr, k=k)


def not_bullet(line):
    patt = [
        r"0", r"[0-9]+ +[0-9~个只-]", r"[0-9]+\.{2,}"
    ]
    return any([re.match(r, line) for r in patt])


def bullets_category(sections):
    global BULLET_PATTERN
    hits = [0] * len(BULLET_PATTERN)
    for i, pro in enumerate(BULLET_PATTERN):
        for sec in sections:
            sec = sec.strip()
            for p in pro:
                if re.match(p, sec) and not not_bullet(sec):
                    hits[i] += 1
                    break
    maximum = 0
    res = -1
    for i, h in enumerate(hits):
        if h <= maximum:
            continue
        res = i
        maximum = h
    return res


def is_english(texts):
    if not texts:
        return False

    pattern = re.compile(r"[`a-zA-Z0-9\s.,':;/\"?<>!\(\)\-]")

    if isinstance(texts, str):
        texts = list(texts)
    elif isinstance(texts, list):
        texts = [t for t in texts if isinstance(t, str) and t.strip()]
    else:
        return False

    if not texts:
        return False

    eng = sum(1 for t in texts if pattern.fullmatch(t.strip()))
    return (eng / len(texts)) > 0.8


def is_chinese(text):
    if not text:
        return False
    chinese = 0
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff':
            chinese += 1
    if chinese / len(text) > 0.2:
        return True
    return False


def tokenize(d, txt, eng):
    from . import rag_tokenizer
    d["content_with_weight"] = txt
    t = re.sub(r"</?(table|td|caption|tr|th)( [^<>]{0,12})?>", " ", txt)
    d["content_ltks"] = rag_tokenizer.tokenize(t)
    d["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(d["content_ltks"])


def tokenize_chunks(chunks, doc, eng, pdf_parser=None, child_delimiters_pattern=None):
    res = []
    # wrap up as es documents
    for ii, ck in enumerate(chunks):
        if len(ck.strip()) == 0:
            continue
        logging.debug("-- {}".format(ck))
        d = copy.deepcopy(doc)
        if pdf_parser:
            try:
                d["image"], poss = pdf_parser.crop(ck, need_position=True)
                add_positions(d, poss)
                ck = pdf_parser.remove_tag(ck)
            except NotImplementedError:
                pass
        else:
            add_positions(d, [[ii]*5])

        if child_delimiters_pattern:
            d["mom_with_weight"] = ck
            for txt in re.split(r"(%s)" % child_delimiters_pattern, ck, flags=re.DOTALL):
                dd = copy.deepcopy(d)
                tokenize(dd, txt, eng)
                res.append(dd)
            continue

        tokenize(d, ck, eng)
        res.append(d)
    return res


def tokenize_chunks_with_images(chunks, doc, eng, images, child_delimiters_pattern=None):
    res = []
    # wrap up as es documents
    for ii, (ck, image) in enumerate(zip(chunks, images)):
        if len(ck.strip()) == 0:
            continue
        logging.debug("-- {}".format(ck))
        d = copy.deepcopy(doc)
        d["image"] = image
        add_positions(d, [[ii]*5])
        if child_delimiters_pattern:
            d["mom_with_weight"] = ck
            for txt in re.split(r"(%s)" % child_delimiters_pattern, ck, flags=re.DOTALL):
                dd = copy.deepcopy(d)
                tokenize(dd, txt, eng)
                res.append(dd)
            continue
        tokenize(d, ck, eng)
        res.append(d)
    return res


def tokenize_table(tbls, doc, eng, batch_size=10):
    res = []
    # add tables
    for (img, rows), poss in tbls:
        if not rows:
            continue
        if isinstance(rows, str):
            d = copy.deepcopy(doc)
            tokenize(d, rows, eng)
            d["content_with_weight"] = rows
            d["doc_type_kwd"] = "table"
            if img:
                d["image"] = img
                d["doc_type_kwd"] = "image"
            if poss:
                add_positions(d, poss)
            res.append(d)
            continue
        de = "; " if eng else "； "
        for i in range(0, len(rows), batch_size):
            d = copy.deepcopy(doc)
            r = de.join(rows[i:i + batch_size])
            tokenize(d, r, eng)
            d["doc_type_kwd"] = "table"
            if img:
                d["image"] = img
                d["doc_type_kwd"] = "image"
            add_positions(d, poss)
            res.append(d)
    return res


def attach_media_context(chunks, table_context_size=0, image_context_size=0):
    """
    为图片和表格块添加上下文文本（前后相邻的文本块）
    
    功能说明：
    - 为图片块添加前后相邻的文本块作为上下文（受 image_context_size 限制）
    - 为表格块添加前后相邻的文本块作为上下文（受 table_context_size 限制）
    - 只从文本块中提取上下文，不会从其他图片/表格块中提取
    - 如果有位置信息，会先按位置排序，确保上下文来自真正相邻的块
    
    Args:
        chunks: 块列表，每个元素是一个字典，包含 text/content_with_weight、image、doc_type_kwd 等字段
        table_context_size: 表格上下文大小（token 数），前后各添加这么多 token 的文本
        image_context_size: 图片上下文大小（token 数），前后各添加这么多 token 的文本
    
    Returns:
        list: 修改后的块列表（原地修改，也会返回）
    """
    from . import rag_tokenizer
    
    # 如果块列表为空，或者两个上下文大小都为 0，直接返回
    if not chunks or (table_context_size <= 0 and image_context_size <= 0):
        return chunks

    def is_image_chunk(ck):
        """判断是否为图片块"""
        # 如果明确标记为图片类型，返回 True
        if ck.get("doc_type_kwd") == "image":
            return True
        # 如果有图片但没有文本，也认为是图片块
        text_val = ck.get("content_with_weight") if isinstance(ck.get("content_with_weight"), str) else ck.get("text")
        has_text = isinstance(text_val, str) and text_val.strip()
        return bool(ck.get("image")) and not has_text

    def is_table_chunk(ck):
        """判断是否为表格块"""
        return ck.get("doc_type_kwd") == "table"

    def is_text_chunk(ck):
        """判断是否为文本块（既不是图片也不是表格）"""
        return not is_image_chunk(ck) and not is_table_chunk(ck)

    def get_text(ck):
        """从块中提取文本内容"""
        # 优先使用 content_with_weight，否则使用 text
        if isinstance(ck.get("content_with_weight"), str):
            return ck["content_with_weight"]
        if isinstance(ck.get("text"), str):
            return ck["text"]
        return ""

    def split_sentences(text):
        """将文本按句子分割（支持中英文标点）"""
        # 匹配句子结束符：.。！？!?；;：:\n
        pattern = r"([.。！？!?；;：:\n])"
        parts = re.split(pattern, text)
        sentences = []
        buf = ""
        for p in parts:
            if not p:
                continue
            # 如果是标点符号，添加到缓冲区并完成一个句子
            if re.fullmatch(pattern, p):
                buf += p
                sentences.append(buf)
                buf = ""
            else:
                # 否则继续累积到缓冲区
                buf += p
        # 处理最后一个句子（如果没有以标点结尾）
        if buf:
            sentences.append(buf)
        return sentences

    def trim_to_tokens(text, token_budget, from_tail=False):
        """
        根据 token 预算截断文本
        
        Args:
            text: 要截断的文本
            token_budget: token 预算（最大 token 数）
            from_tail: 如果为 True，从尾部开始截取；否则从头部开始
        
        Returns:
            str: 截断后的文本
        """
        if token_budget <= 0 or not text:
            return ""
        sentences = split_sentences(text)
        if not sentences:
            return ""

        collected = []
        remaining = token_budget
        # 根据 from_tail 决定遍历顺序
        seq = reversed(sentences) if from_tail else sentences
        for s in seq:
            tks = num_tokens_from_string(s)
            if tks <= 0:
                continue
            # 如果当前句子超过剩余预算，只取这个句子并结束
            if tks > remaining:
                collected.append(s)
                break
            # 否则添加到收集列表，并减少剩余预算
            collected.append(s)
            remaining -= tks

        # 如果从尾部截取，需要反转回来
        if from_tail:
            collected = list(reversed(collected))
        return "".join(collected)

    def extract_position(ck):
        """
        从块中提取位置信息（页码、垂直位置、水平位置）
        
        Returns:
            tuple: (页码, top, left) 或 (None, None, None)
        """
        pn = None
        top = None
        left = None
        try:
            # 尝试从不同字段获取页码
            if ck.get("page_num_int"):
                pn = ck["page_num_int"][0]
            elif ck.get("page_number") is not None:
                pn = ck.get("page_number")

            # 尝试从不同字段获取垂直位置（top）
            if ck.get("top_int"):
                top = ck["top_int"][0]
            elif ck.get("top") is not None:
                top = ck.get("top")

            # 尝试从不同字段获取水平位置（left/x0）
            if ck.get("position_int"):
                left = ck["position_int"][0][1]
            elif ck.get("x0") is not None:
                left = ck.get("x0")
        except Exception:
            pn = top = left = None
        return pn, top, left

    # ========== 第一步：按位置排序块 ==========
    # 为每个块建立索引，并分类为有位置信息和无位置信息
    indexed = list(enumerate(chunks))
    positioned_indices = []  # 有位置信息的块索引列表
    unpositioned_indices = []  # 无位置信息的块索引列表
    
    for idx, ck in indexed:
        pn, top, left = extract_position(ck)
        # 如果有页码和垂直位置，认为有位置信息
        if pn is not None and top is not None:
            positioned_indices.append((idx, pn, top, left if left is not None else 0))
        else:
            unpositioned_indices.append(idx)

    # 如果有位置信息，按位置排序：先按页码，再按 top（垂直位置），再按 left（水平位置），最后按原始索引
    if positioned_indices:
        positioned_indices.sort(key=lambda x: (int(x[1]), int(x[2]), int(x[3]), x[0]))
        # 有位置信息的块在前，无位置信息的块在后
        ordered_indices = [i for i, _, _, _ in positioned_indices] + unpositioned_indices
    else:
        # 如果没有位置信息，保持原始顺序
        ordered_indices = [idx for idx, _ in indexed]

    # ========== 第二步：为每个图片/表格块添加上下文 ==========
    total = len(ordered_indices)
    for sorted_pos, idx in enumerate(ordered_indices):
        ck = chunks[idx]
        
        # 确定当前块需要的上下文大小
        token_budget = image_context_size if is_image_chunk(ck) else table_context_size if is_table_chunk(ck) else 0
        if token_budget <= 0:
            continue  # 如果不需要上下文，跳过

        # ========== 向前查找上下文（前面的文本块） ==========
        prev_ctx = []  # 前面的上下文文本列表
        remaining_prev = token_budget  # 剩余的 token 预算
        
        # 从当前位置向前遍历
        for prev_idx in range(sorted_pos - 1, -1, -1):
            if remaining_prev <= 0:
                break  # 预算用完，停止查找
            neighbor_idx = ordered_indices[prev_idx]
            # 只从文本块中提取上下文，遇到图片/表格块就停止
            if not is_text_chunk(chunks[neighbor_idx]):
                break
            txt = get_text(chunks[neighbor_idx])
            if not txt:
                continue
            tks = num_tokens_from_string(txt)
            if tks <= 0:
                continue
            # 如果文本超过剩余预算，从尾部截断
            if tks > remaining_prev:
                txt = trim_to_tokens(txt, remaining_prev, from_tail=True)
                tks = num_tokens_from_string(txt)
            prev_ctx.append(txt)
            remaining_prev -= tks
        # 反转列表，因为是从后往前收集的
        prev_ctx.reverse()

        # ========== 向后查找上下文（后面的文本块） ==========
        next_ctx = []  # 后面的上下文文本列表
        remaining_next = token_budget  # 剩余的 token 预算
        
        # 从当前位置向后遍历
        for next_idx in range(sorted_pos + 1, total):
            if remaining_next <= 0:
                break  # 预算用完，停止查找
            neighbor_idx = ordered_indices[next_idx]
            # 只从文本块中提取上下文，遇到图片/表格块就停止
            if not is_text_chunk(chunks[neighbor_idx]):
                break
            txt = get_text(chunks[neighbor_idx])
            if not txt:
                continue
            tks = num_tokens_from_string(txt)
            if tks <= 0:
                continue
            # 如果文本超过剩余预算，从头部截断
            if tks > remaining_next:
                txt = trim_to_tokens(txt, remaining_next, from_tail=False)
                tks = num_tokens_from_string(txt)
            next_ctx.append(txt)
            remaining_next -= tks

        # 如果没有找到任何上下文，跳过
        if not prev_ctx and not next_ctx:
            continue

        # ========== 合并上下文和当前块文本 ==========
        self_text = get_text(ck)
        pieces = [*prev_ctx]  # 前面的上下文
        if self_text:
            pieces.append(self_text)  # 当前块文本
        pieces.extend(next_ctx)  # 后面的上下文
        combined = "\n".join(pieces)  # 用换行符连接

        # ========== 更新块的内容 ==========
        original = ck.get("content_with_weight")
        # 优先更新 content_with_weight，否则更新 text
        if "content_with_weight" in ck:
            ck["content_with_weight"] = combined
        elif "text" in ck:
            original = ck.get("text")
            ck["text"] = combined

        # 如果内容有变化，重新计算 token 化结果
        if combined != original:
            if "content_ltks" in ck:
                ck["content_ltks"] = rag_tokenizer.tokenize(combined)
            if "content_sm_ltks" in ck:
                ck["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(ck.get("content_ltks", rag_tokenizer.tokenize(combined)))

    # ========== 第三步：如果有位置信息，按排序后的顺序重新排列块 ==========
    if positioned_indices:
        chunks[:] = [chunks[i] for i in ordered_indices]

    return chunks


def add_positions(d, poss):
    if not poss:
        return
    page_num_int = []
    position_int = []
    top_int = []
    for pn, left, right, top, bottom in poss:
        page_num_int.append(int(pn + 1))
        top_int.append(int(top))
        position_int.append((int(pn + 1), int(left), int(right), int(top), int(bottom)))
    d["page_num_int"] = page_num_int
    d["position_int"] = position_int
    d["top_int"] = top_int


def remove_contents_table(sections, eng=False):
    i = 0
    while i < len(sections):
        def get(i):
            nonlocal sections
            return (sections[i] if isinstance(sections[i],
                                              type("")) else sections[i][0]).strip()

        if not re.match(r"(contents|目录|目次|table of contents|致谢|acknowledge)$",
                        re.sub(r"( | |\u3000)+", "", get(i).split("@@")[0], flags=re.IGNORECASE)):
            i += 1
            continue
        sections.pop(i)
        if i >= len(sections):
            break
        prefix = get(i)[:3] if not eng else " ".join(get(i).split()[:2])
        while not prefix:
            sections.pop(i)
            if i >= len(sections):
                break
            prefix = get(i)[:3] if not eng else " ".join(get(i).split()[:2])
        sections.pop(i)
        if i >= len(sections) or not prefix:
            break
        for j in range(i, min(i + 128, len(sections))):
            if not re.match(prefix, get(j)):
                continue
            for _ in range(i, j):
                sections.pop(i)
            break


def make_colon_as_title(sections):
    if not sections:
        return []
    if isinstance(sections[0], type("")):
        return sections
    i = 0
    while i < len(sections):
        txt, layout = sections[i]
        i += 1
        txt = txt.split("@")[0].strip()
        if not txt:
            continue
        if txt[-1] not in ":：":
            continue
        txt = txt[::-1]
        arr = re.split(r"([。？！!?;；]| \.)", txt)
        if len(arr) < 2 or len(arr[1]) < 32:
            continue
        sections.insert(i - 1, (arr[0][::-1], "title"))
        i += 1


def title_frequency(bull, sections):
    bullets_size = len(BULLET_PATTERN[bull])
    levels = [bullets_size + 1 for _ in range(len(sections))]
    if not sections or bull < 0:
        return bullets_size + 1, levels

    for i, (txt, layout) in enumerate(sections):
        for j, p in enumerate(BULLET_PATTERN[bull]):
            if re.match(p, txt.strip()) and not not_bullet(txt):
                levels[i] = j
                break
        else:
            if re.search(r"(title|head)", layout) and not not_title(txt.split("@")[0]):
                levels[i] = bullets_size
    most_level = bullets_size + 1
    for level, c in sorted(Counter(levels).items(), key=lambda x: x[1] * -1):
        if level <= bullets_size:
            most_level = level
            break
    return most_level, levels


def not_title(txt):
    if re.match(r"第[零一二三四五六七八九十百0-9]+条", txt):
        return False
    if len(txt.split()) > 12 or (txt.find(" ") < 0 and len(txt) >= 32):
        return True
    return re.search(r"[,;，。；！!]", txt)

def tree_merge(bull, sections, depth):

    if not sections or bull < 0:
        return sections
    if isinstance(sections[0], type("")):
        sections = [(s, "") for s in sections]

    # filter out position information in pdf sections
    sections = [(t, o) for t, o in sections if
                t and len(t.split("@")[0].strip()) > 1 and not re.match(r"[0-9]+$", t.split("@")[0].strip())]

    def get_level(bull, section):
        text, layout = section
        text = re.sub(r"\u3000", " ",   text).strip()

        for i, title in enumerate(BULLET_PATTERN[bull]):
            if re.match(title, text.strip()):
                return i+1, text
        else:
            if re.search(r"(title|head)", layout) and not not_title(text):
                return len(BULLET_PATTERN[bull])+1, text
            else:
                return len(BULLET_PATTERN[bull])+2, text
    level_set = set()
    lines = []
    for section in sections:
        level, text = get_level(bull, section)
        if not text.strip("\n"):
            continue

        lines.append((level, text))
        level_set.add(level)

    sorted_levels = sorted(list(level_set))

    if depth <= len(sorted_levels):
        target_level = sorted_levels[depth - 1]
    else:
        target_level = sorted_levels[-1]

    if target_level == len(BULLET_PATTERN[bull]) + 2:
        target_level = sorted_levels[-2] if len(sorted_levels) > 1 else sorted_levels[0]

    root = Node(level=0, depth=target_level, texts=[])
    root.build_tree(lines)

    return [element for element in root.get_tree() if element]

def hierarchical_merge(bull, sections, depth):

    if not sections or bull < 0:
        return []
    if isinstance(sections[0], type("")):
        sections = [(s, "") for s in sections]
    sections = [(t, o) for t, o in sections if
                t and len(t.split("@")[0].strip()) > 1 and not re.match(r"[0-9]+$", t.split("@")[0].strip())]
    bullets_size = len(BULLET_PATTERN[bull])
    levels = [[] for _ in range(bullets_size + 2)]

    for i, (txt, layout) in enumerate(sections):
        for j, p in enumerate(BULLET_PATTERN[bull]):
            if re.match(p, txt.strip()):
                levels[j].append(i)
                break
        else:
            if re.search(r"(title|head)", layout) and not not_title(txt):
                levels[bullets_size].append(i)
            else:
                levels[bullets_size + 1].append(i)
    sections = [t for t, _ in sections]

    # for s in sections: print("--", s)

    def binary_search(arr, target):
        if not arr:
            return -1
        if target > arr[-1]:
            return len(arr) - 1
        if target < arr[0]:
            return -1
        s, e = 0, len(arr)
        while e - s > 1:
            i = (e + s) // 2
            if target > arr[i]:
                s = i
                continue
            elif target < arr[i]:
                e = i
                continue
            else:
                assert False
        return s

    cks = []
    readed = [False] * len(sections)
    levels = levels[::-1]
    for i, arr in enumerate(levels[:depth]):
        for j in arr:
            if readed[j]:
                continue
            readed[j] = True
            cks.append([j])
            if i + 1 == len(levels) - 1:
                continue
            for ii in range(i + 1, len(levels)):
                jj = binary_search(levels[ii], j)
                if jj < 0:
                    continue
                if levels[ii][jj] > cks[-1][-1]:
                    cks[-1].pop(-1)
                cks[-1].append(levels[ii][jj])
            for ii in cks[-1]:
                readed[ii] = True

    if not cks:
        return cks

    for i in range(len(cks)):
        cks[i] = [sections[j] for j in cks[i][::-1]]
        logging.debug("\n* ".join(cks[i]))

    res = [[]]
    num = [0]
    for ck in cks:
        if len(ck) == 1:
            n = num_tokens_from_string(re.sub(r"@@[0-9]+.*", "", ck[0]))
            if n + num[-1] < 218:
                res[-1].append(ck[0])
                num[-1] += n
                continue
            res.append(ck)
            num.append(n)
            continue
        res.append(ck)
        num.append(218)

    return res


def naive_merge(sections: str | list, chunk_token_num=128, delimiter="\n。；！？", overlapped_percent=0):
    from deepdoc.parser.pdf_parser import RAGFlowPdfParser
    if not sections:
        return []
    if isinstance(sections, str):
        sections = [sections]
    if isinstance(sections[0], str):
        sections = [(s, "") for s in sections]
    cks = [""]
    tk_nums = [0]

    def add_chunk(t, pos):
        nonlocal cks, tk_nums, delimiter
        tnum = num_tokens_from_string(t)
        if not pos:
            pos = ""
        if tnum < 8:
            pos = ""
        # Ensure that the length of the merged chunk does not exceed chunk_token_num
        if cks[-1] == "" or tk_nums[-1] > chunk_token_num * (100 - overlapped_percent)/100.:
            if cks:
                overlapped = RAGFlowPdfParser.remove_tag(cks[-1])
                t = overlapped[int(len(overlapped)*(100-overlapped_percent)/100.):] + t
            if t.find(pos) < 0:
                t += pos
            cks.append(t)
            tk_nums.append(tnum)
        else:
            if cks[-1].find(pos) < 0:
                t += pos
            cks[-1] += t
            tk_nums[-1] += tnum

    custom_delimiters = [m.group(1) for m in re.finditer(r"`([^`]+)`", delimiter)]
    has_custom = bool(custom_delimiters)
    if has_custom:
        custom_pattern = "|".join(re.escape(t) for t in sorted(set(custom_delimiters), key=len, reverse=True))
        cks, tk_nums = [], []
        for sec, pos in sections:
            split_sec = re.split(r"(%s)" % custom_pattern, sec, flags=re.DOTALL)
            for sub_sec in split_sec:
                if re.fullmatch(custom_pattern, sub_sec or ""):
                    continue
                text = "\n" + sub_sec
                local_pos = pos
                if num_tokens_from_string(text) < 8:
                    local_pos = ""
                if local_pos and text.find(local_pos) < 0:
                    text += local_pos
                cks.append(text)
                tk_nums.append(num_tokens_from_string(text))
        return cks

    for sec, pos in sections:
        add_chunk("\n"+sec, pos)

    return cks


def naive_merge_with_images(texts, images, chunk_token_num=128, delimiter="\n。；！？", overlapped_percent=0):
    """
    将多个 sections（文本+图片）合并成 chunks
    
    功能说明：
    - 按照 chunk_token_num 限制，将多个 sections 合并成 chunks
    - 如果当前 chunk 的 token 数未达到上限，继续添加新的 section
    - 如果当前 chunk 的 token 数达到上限，创建新的 chunk
    - 图片会被垂直拼接（concat_img）到同一个 chunk 中
    - 支持自定义分隔符，可以按分隔符强制分割
    
    Args:
        texts: 文本列表，每个元素可以是字符串或元组 (文本, 位置标签)
        images: 图片列表，每个元素是 PIL.Image 对象或 None
        chunk_token_num: 每个 chunk 的最大 token 数
        delimiter: 分隔符字符串，支持自定义分隔符（用反引号包裹，如 `\n\n`）
        overlapped_percent: 重叠百分比（0-100），用于控制 chunk 之间的重叠
    
    步骤：
    1. 初始化 chunks 列表
    2. 遍历所有 sections
    3. 调用 add_chunk() 添加到 chunk
    
    """
    from deepdoc.parser.pdf_parser import RAGFlowPdfParser
    
    # 参数验证：文本和图片数量必须一致
    if not texts or len(texts) != len(images):
        return [], []
    
    # 初始化：chunks 列表、图片列表、token 数列表
    cks = [""]  # 当前只有一个空的 chunk
    result_images = [None]  # 对应的图片列表
    tk_nums = [0]  # 每个 chunk 的 token 数

    def add_chunk(t, image, pos=""):
        """
        核心分块函数：将文本和图片添加到 chunk 中
        
        关键逻辑：
        1. 计算文本的 token 数
        2. 判断是否需要创建新 chunk：
           - 如果当前 chunk 为空，或
           - 如果当前 chunk 的 token 数 > chunk_token_num * (100 - overlapped_percent)/100
           则创建新 chunk
        3. 否则合并到当前 chunk：
           - 文本追加到当前 chunk
           - 图片垂直拼接（concat_img）
        
        Args:
            t: 文本内容
            image: 图片对象（PIL.Image 或 None）
            pos: 位置标签字符串（格式：@@页码\tx0\tx1\ttop\tbottom##）
        
        步骤： 
        1. 计算文本的 token 数
        2. 判断是否需要创建新 chunk
        3. 创建新 chunk 或合并到当前 chunk
        """
        nonlocal cks, result_images, tk_nums, delimiter
        
        # 计算文本的 token 数
        tnum = num_tokens_from_string(t)
        
        # 处理位置标签：如果没有提供，使用空字符串
        if not pos:
            pos = ""
        # 如果文本太短（< 8 tokens），不添加位置标签
        if tnum < 8:
            pos = ""
        
        # ========== 判断是否需要创建新 chunk ==========
        # 判断条件：当前 chunk 为空，或 token 数超过限制
        # 限制 = chunk_token_num * (100 - overlapped_percent) / 100
        # 例如：chunk_token_num=128, overlapped_percent=0，限制为 128
        #      chunk_token_num=128, overlapped_percent=10，限制为 115.2
        if cks[-1] == "" or tk_nums[-1] > chunk_token_num * (100 - overlapped_percent)/100.:
            # ========== 创建新 chunk ==========
            # 如果有重叠，从上一个 chunk 的末尾提取重叠部分
            if cks:
                overlapped = RAGFlowPdfParser.remove_tag(cks[-1])
                # 计算重叠部分的起始位置
                overlap_start = int(len(overlapped) * (100 - overlapped_percent) / 100.)
                # 将重叠部分添加到新文本的开头
                t = overlapped[overlap_start:] + t
            
            # 添加位置标签（如果还没有）
            if t.find(pos) < 0:
                t += pos
            
            # 创建新 chunk
            cks.append(t)
            result_images.append(image)
            tk_nums.append(tnum)
        else:
            # ========== 合并到当前 chunk ==========
            # 添加位置标签（如果还没有）
            if cks[-1].find(pos) < 0:
                t += pos
            
            # 文本追加到当前 chunk
            cks[-1] += t
            
            # 图片处理：如果当前 chunk 没有图片，直接赋值；否则垂直拼接
            if result_images[-1] is None:
                result_images[-1] = image
            else:
                # 垂直拼接图片：将新图片追加到现有图片下方
                result_images[-1] = concat_img(result_images[-1], image)
            
            # 更新 token 数
            tk_nums[-1] += tnum

    # ========== 处理自定义分隔符 ==========
    # 从 delimiter 中提取自定义分隔符（用反引号包裹的部分，如 `\n\n`）
    custom_delimiters = [m.group(1) for m in re.finditer(r"`([^`]+)`", delimiter)]
    has_custom = bool(custom_delimiters)
    
    if has_custom:
        # 如果有自定义分隔符，按分隔符强制分割
        # 构建正则表达式模式（按长度降序排列，确保长模式优先匹配）
        custom_pattern = "|".join(re.escape(t) for t in sorted(set(custom_delimiters), key=len, reverse=True))
        
        # 重新初始化
        cks, result_images, tk_nums = [], [], []
        
        # 遍历每个 section
        for text, image in zip(texts, images):
            # 解包文本和位置标签
            text_str = text[0] if isinstance(text, tuple) else text
            text_pos = text[1] if isinstance(text, tuple) and len(text) > 1 else ""
            
            # 按自定义分隔符分割文本
            split_sec = re.split(r"(%s)" % custom_pattern, text_str)
            
            # 处理每个分割后的片段
            for sub_sec in split_sec:
                # 跳过分隔符本身
                if re.fullmatch(custom_pattern, sub_sec or ""):
                    continue
                
                # 构建文本片段
                text_seg = "\n" + sub_sec
                local_pos = text_pos
                
                # 如果文本太短，不添加位置标签
                if num_tokens_from_string(text_seg) < 8:
                    local_pos = ""
                
                # 添加位置标签（如果还没有）
                if local_pos and text_seg.find(local_pos) < 0:
                    text_seg += local_pos
                
                # 每个片段都创建一个独立的 chunk
                cks.append(text_seg)
                result_images.append(image)
                tk_nums.append(num_tokens_from_string(text_seg))
        
        return cks, result_images

    # ========== 正常合并流程（无自定义分隔符） ==========
    # 遍历所有 sections
    for text, image in zip(texts, images):
        # 如果 text 是元组，解包为 (文本, 位置标签)
        if isinstance(text, tuple):
            text_str = text[0]
            text_pos = text[1] if len(text) > 1 else ""
            add_chunk("\n" + text_str, image, text_pos)
        else:
            # 否则直接使用文本
            add_chunk("\n" + text, image)

    return cks, result_images


def docx_question_level(p, bull=-1):
    txt = re.sub(r"\u3000", " ", p.text).strip()
    if p.style.name.startswith('Heading'):
        return int(p.style.name.split(' ')[-1]), txt
    else:
        if bull < 0:
            return 0, txt
        for j, title in enumerate(BULLET_PATTERN[bull]):
            if re.match(title, txt):
                return j + 1, txt
    return len(BULLET_PATTERN[bull])+1, txt


def concat_img(img1, img2):
    if img1 and not img2:
        return img1
    if not img1 and img2:
        return img2
    if not img1 and not img2:
        return None

    if img1 is img2:
        return img1

    if isinstance(img1, Image.Image) and isinstance(img2, Image.Image):
        pixel_data1 = img1.tobytes()
        pixel_data2 = img2.tobytes()
        if pixel_data1 == pixel_data2:
            return img1

    width1, height1 = img1.size
    width2, height2 = img2.size

    new_width = max(width1, width2)
    new_height = height1 + height2
    new_image = Image.new('RGB', (new_width, new_height))

    new_image.paste(img1, (0, 0))
    new_image.paste(img2, (0, height1))
    return new_image


def naive_merge_docx(sections, chunk_token_num=128, delimiter="\n。；！？"):
    if not sections:
        return [], []

    cks = []
    images = []
    tk_nums = []

    def add_chunk(t, image, pos=""):
        nonlocal cks, images, tk_nums
        tnum = num_tokens_from_string(t)
        if tnum < 8:
            pos = ""

        if not cks or tk_nums[-1] > chunk_token_num:
            # new chunk
            if pos and t.find(pos) < 0:
                t += pos
            cks.append(t)
            images.append(image)
            tk_nums.append(tnum)
        else:
            # add to last chunk
            if pos and cks[-1].find(pos) < 0:
                t += pos
            cks[-1] += t
            images[-1] = concat_img(images[-1], image)
            tk_nums[-1] += tnum

    custom_delimiters = [m.group(1) for m in re.finditer(r"`([^`]+)`", delimiter)]
    has_custom = bool(custom_delimiters)
    if has_custom:
        custom_pattern = "|".join(re.escape(t) for t in sorted(set(custom_delimiters), key=len, reverse=True))
        cks, images, tk_nums = [], [], []
        pattern = r"(%s)" % custom_pattern
        for sec, image in sections:
            split_sec = re.split(pattern, sec)
            for sub_sec in split_sec:
                if not sub_sec or re.fullmatch(custom_pattern, sub_sec):
                    continue
                text_seg = "\n" + sub_sec
                cks.append(text_seg)
                images.append(image)
                tk_nums.append(num_tokens_from_string(text_seg))
        return cks, images

    for sec, image in sections:
        add_chunk("\n" + sec, image, "")

    return cks, images


def extract_between(text: str, start_tag: str, end_tag: str) -> list[str]:
    pattern = re.escape(start_tag) + r"(.*?)" + re.escape(end_tag)
    return re.findall(pattern, text, flags=re.DOTALL)


def get_delimiters(delimiters: str):
    dels = []
    s = 0
    for m in re.finditer(r"`([^`]+)`", delimiters, re.I):
        f, t = m.span()
        dels.append(m.group(1))
        dels.extend(list(delimiters[s: f]))
        s = t
    if s < len(delimiters):
        dels.extend(list(delimiters[s:]))

    dels.sort(key=lambda x: -len(x))
    dels = [re.escape(d) for d in dels if d]
    dels = [d for d in dels if d]
    dels_pattern = "|".join(dels)

    return dels_pattern


class Node:
    def __init__(self, level, depth=-1, texts=None):
        self.level = level
        self.depth = depth
        self.texts = texts or []
        self.children = []

    def add_child(self, child_node):
        self.children.append(child_node)

    def get_children(self):
        return self.children

    def get_level(self):
        return self.level

    def get_texts(self):
        return self.texts

    def set_texts(self, texts):
        self.texts = texts

    def add_text(self, text):
        self.texts.append(text)

    def clear_text(self):
        self.texts = []

    def __repr__(self):
        return f"Node(level={self.level}, texts={self.texts}, children={len(self.children)})"

    def build_tree(self, lines):
        stack = [self]
        for level, text in lines:
            if self.depth != -1 and level > self.depth:
                # Beyond target depth: merge content into the current leaf instead of creating deeper nodes
                stack[-1].add_text(text)
                continue

            # Move up until we find the proper parent whose level is strictly smaller than current
            while len(stack) > 1 and level <= stack[-1].get_level():
                stack.pop()

            node = Node(level=level, texts=[text])
            # Attach as child of current parent and descend
            stack[-1].add_child(node)
            stack.append(node)

        return self

    def get_tree(self):
        tree_list = []
        self._dfs(self, tree_list, [])
        return tree_list

    def _dfs(self, node, tree_list, titles):
        level = node.get_level()
        texts = node.get_texts()
        child = node.get_children()

        if level == 0 and texts:
            tree_list.append("\n".join(titles+texts))

        # Titles within configured depth are accumulated into the current path
        if 1 <= level <= self.depth:
            path_titles = titles + texts
        else:
            path_titles = titles

        # Body outside the depth limit becomes its own chunk under the current title path
        if level > self.depth and texts:
            tree_list.append("\n".join(path_titles + texts))

        # A leaf title within depth emits its title path as a chunk (header-only section)
        elif not child and (1 <= level <= self.depth):
            tree_list.append("\n".join(path_titles))

        # Recurse into children with the updated title path
        for c in child:
            self._dfs(c, tree_list, path_titles)
