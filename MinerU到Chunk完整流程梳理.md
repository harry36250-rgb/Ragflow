# MinerU 到 Chunk 完整流程梳理

## 一、MinerU 输出 → Sections 转换

### 1.1 转换位置
**文件：** `deepdoc/parser/mineru_parser.py`  
**方法：** `_transfer_to_sections()`

### 1.2 转换规则

MinerU 解析后输出一个 `outputs` 列表，每个元素是一个字典，包含：
- `type`: 内容类型（TEXT/IMAGE/TABLE/EQUATION/CODE/LIST/DISCARDED）
- `text`: 文本内容（TEXT 类型）
- `img_path`: 图片路径（IMAGE 类型）
- `image_caption`: 图片标题列表（IMAGE 类型）
- `image_footnote`: 图片脚注列表（IMAGE 类型）
- `bbox`: 边界框 `[x0, top, x1, bottom]`
- `page_idx`: 页码（从 0 开始）

**转换逻辑：**

```python
for output in outputs:
    match output["type"]:
        case MinerUContentType.TEXT:
            section = output["text"]  # 直接使用文本
        
        case MinerUContentType.IMAGE:
            # 图片类型：组合图片标题和脚注
            caption = "".join(output.get("image_caption", [])) or processed_result
            section = caption + "\n" + "".join(output.get("image_footnote", []))
        
        case MinerUContentType.TABLE:
            # 表格类型：组合表格主体、标题和脚注
            section = table_body + table_caption + table_footnote
        
        # ... 其他类型
    
    # 生成位置标签：@@页码\tx0\tx1\ttop\tbottom##
    position_tag = self._line_tag(output)
    
    # 默认模式：返回 (文本内容, 位置标签)
    sections.append((section, position_tag))
```

**关键点：**
- **一对一映射**：每个 MinerU 输出块对应一个 section
- **位置标签**：每个 section 都带有位置信息 `@@页码\tx0\tx1\ttop\tbottom##`
- **图片处理**：图片的 section 包含 caption + footnote，如果 caption 为空，会调用 Dify 生成

---

## 二、Sections → Bboxes 转换

### 2.1 转换位置
**文件：** `rag/flow/parser/parser.py`  
**方法：** `_pdf()` (MinerU 分支)

### 2.2 转换规则

```python
# MinerU 返回 lines: [(文本内容, 位置标签), ...]
lines, _ = pdf_parser.parse_pdf(...)

# 转换为 bboxes
bboxes = []
for t, poss in lines:  # t: 文本内容, poss: 位置标签字符串
    box = {
        "image": pdf_parser.crop(poss, 1),  # 根据位置标签裁剪图片
        "positions": [[页码, x0, x1, top, bottom], ...],  # 提取位置信息
        "text": t,  # 文本内容
    }
    bboxes.append(box)
```

**关键点：**
- **一对一映射**：每个 section 对应一个 bbox
- **图片裁剪**：`crop()` 方法根据位置标签从 PDF 页面中裁剪出对应的图片区域
- **位置提取**：从位置标签字符串中解析出 `[页码, x0, x1, top, bottom]` 列表

### 2.3 分类标记

```python
# 对每个 bbox 进行分类
for b in bboxes:
    text_val = b.get("text", "")
    has_text = isinstance(text_val, str) and text_val.strip()
    layout = b.get("layout_type")
    
    # 标记为图片块
    if layout == "figure" or (b.get("image") and not has_text):
        b["doc_type_kwd"] = "image"
    
    # 标记为表格块
    elif layout == "table":
        b["doc_type_kwd"] = "table"
```

---

## 三、Bboxes 合并规则（attach_media_context）

### 3.1 合并位置
**文件：** `rag/nlp/__init__.py`  
**方法：** `attach_media_context()`

### 3.2 合并规则

**目的：** 为图片和表格块添加上下文文本（前后相邻的文本块）

**触发条件：**
- `table_context_size > 0`：为表格添加上下文
- `image_context_size > 0`：为图片添加上下文

**合并逻辑：**

```python
# 1. 按位置排序（如果有位置信息）
# 排序规则：先按页码，再按 top（垂直位置），再按 x0（水平位置）
positioned_indices.sort(key=lambda x: (页码, top, x0, 原始索引))

# 2. 为每个图片/表格块添加上下文
for 当前块 in bboxes:
    if 当前块是图片块 and image_context_size > 0:
        # 向前查找文本块（最多 image_context_size 个 token）
        prev_ctx = []
        for 前一个块 in 前面的块列表:
            if 前一个块是文本块:
                prev_ctx.append(前一个块的文本)
                # 如果超过 token 限制，截断
                if token数 > 剩余预算:
                    prev_ctx[-1] = 截断文本
                    break
        
        # 向后查找文本块（最多 image_context_size 个 token）
        next_ctx = []
        for 后一个块 in 后面的块列表:
            if 后一个块是文本块:
                next_ctx.append(后一个块的文本)
                # 如果超过 token 限制，截断
                if token数 > 剩余预算:
                    next_ctx[-1] = 截断文本
                    break
        
        # 合并：前文 + 当前块文本 + 后文
        当前块["text"] = "\n".join([*prev_ctx, 当前块文本, *next_ctx])
```

**关键点：**
- **只添加文本块**：只从相邻的文本块中提取上下文，不会从其他图片/表格块中提取
- **Token 限制**：上下文大小受 `image_context_size` 或 `table_context_size` 限制
- **按位置排序**：如果有位置信息，会先按位置排序，确保上下文来自真正相邻的块
- **不改变块数量**：只是修改每个块的内容，不会合并或拆分块

---

## 四、Bboxes → Chunks 转换（naive_merge_with_images）

### 4.1 转换位置
**文件：** `rag/flow/splitter/splitter.py`  
**方法：** `_invoke()` (JSON 格式分支)

### 4.2 转换规则

```python
# 1. 从 bboxes 提取 sections 和 images
sections, section_images = [], []
for bbox in json_result:
    sections.append((bbox.get("text", ""), bbox.get("position_tag", "")))
    section_images.append(bbox.get("img_id"))  # 从对象存储获取图片

# 2. 调用 naive_merge_with_images 合并成 chunks
chunks, images = naive_merge_with_images(
    sections,           # [(文本, 位置标签), ...]
    section_images,     # [图片对象, ...]
    chunk_token_size,   # chunk 的最大 token 数
    delimiter,          # 分隔符
    overlapped_percent, # 重叠百分比
)
```

### 4.3 核心合并逻辑（naive_merge_with_images）

**文件：** `rag/nlp/__init__.py`

**合并规则：**

```python
def add_chunk(t, image, pos=""):
    tnum = num_tokens_from_string(t)  # 计算文本的 token 数
    
    # 判断是否需要创建新 chunk
    if 当前chunk为空 or 当前chunk的token数 > chunk_token_num * (100 - overlapped_percent)/100:
        # 创建新 chunk
        chunks.append(t)
        result_images.append(image)
        tk_nums.append(tnum)
    else:
        # 合并到当前 chunk
        chunks[-1] += t  # 文本追加
        if result_images[-1] is None:
            result_images[-1] = image
        else:
            # 图片合并：垂直拼接
            result_images[-1] = concat_img(result_images[-1], image)
        tk_nums[-1] += tnum

# 遍历所有 sections
for text, image in zip(sections, section_images):
    add_chunk("\n" + text, image, position_tag)
```

**关键点：**

1. **Token 限制判断：**
   - 如果当前 chunk 的 token 数 < `chunk_token_num * (100 - overlapped_percent)/100`
   - 则继续合并到当前 chunk
   - 否则创建新 chunk

2. **文本合并：**
   - 新文本直接追加到当前 chunk 的文本末尾
   - 用 `\n` 分隔

3. **图片合并：**
   - 如果当前 chunk 没有图片，直接赋值
   - 如果当前 chunk 已有图片，使用 `concat_img()` 垂直拼接
   - **这是导致多个图片描述合并在一起的原因！**

4. **MinerU 特殊配置：**
   ```python
   # rag/app/manual.py:249
   if name in ["tcadp", "docling", "mineru"]:
       parser_config["chunk_token_num"] = 0
   ```
   - 理论上 `chunk_token_num = 0` 应该让每个 section 独立成 chunk
   - 但实际判断逻辑 `tk_nums[-1] > chunk_token_num * (100 - overlapped_percent)/100`
   - 当 `chunk_token_num = 0` 时，条件可能不满足，导致继续合并

---

## 五、完整流程图

```
MinerU 解析输出
    ↓
[output1, output2, ..., outputN]
    ↓ _transfer_to_sections()
Sections
    ↓
[(text1, pos1), (text2, pos2), ..., (textN, posN)]
    ↓ parser._pdf() (MinerU 分支)
Bboxes
    ↓
[{text: "...", image: PIL.Image, positions: [...], doc_type_kwd: "image"}, ...]
    ↓ attach_media_context() (如果配置了 image_context_size)
Bboxes (添加上下文)
    ↓
[{text: "前文\n当前文本\n后文", image: PIL.Image, ...}, ...]
    ↓ splitter._invoke() (JSON 格式)
Sections + Section Images
    ↓
[(text1, pos1), ...], [image1, ...]
    ↓ naive_merge_with_images()
Chunks
    ↓
[{text: "合并后的文本", image: 合并后的图片, positions: [...]}, ...]
```

---

## 六、问题分析

### 6.1 为什么图片描述会全部融合在一起？

**原因：**

1. **`naive_merge_with_images` 的合并逻辑：**
   - 只要当前 chunk 的 token 数未达到上限，就会继续合并
   - 即使 `chunk_token_num = 0`，判断条件 `tk_nums[-1] > 0` 可能不满足
   - 导致所有 sections 被合并到一个 chunk

2. **图片合并机制：**
   - `concat_img()` 会将多个图片垂直拼接
   - 所有图片的文本描述也会合并在一起

3. **没有强制分割：**
   - 即使每个图片有独立的 section，也没有机制强制每个 section 独立成 chunk

### 6.2 如何让每个图片独立成 chunk？

**方案 1：修改 `naive_merge_with_images`**
- 检测到图片块时，强制创建新 chunk
- 或者检测到 `doc_type_kwd == "image"` 时，不合并

**方案 2：修改 `splitter.py`**
- 在调用 `naive_merge_with_images` 之前，先按类型分组
- 图片块单独处理，不参与合并

**方案 3：修改判断条件**
- 当 `chunk_token_num = 0` 时，强制每个 section 独立成 chunk

---

## 七、关键代码位置总结

| 阶段 | 文件 | 方法/函数 | 行数 |
|------|------|----------|------|
| MinerU → Sections | `deepdoc/parser/mineru_parser.py` | `_transfer_to_sections()` | 797-855 |
| Sections → Bboxes | `rag/flow/parser/parser.py` | `_pdf()` (MinerU 分支) | 272-302 |
| Bboxes 分类 | `rag/flow/parser/parser.py` | `_pdf()` | 361-371 |
| Bboxes 合并上下文 | `rag/nlp/__init__.py` | `attach_media_context()` | 361-547 |
| Bboxes → Chunks | `rag/flow/splitter/splitter.py` | `_invoke()` | 111-131 |
| 核心合并逻辑 | `rag/nlp/__init__.py` | `naive_merge_with_images()` | 846-913 |
| MinerU 特殊配置 | `rag/app/manual.py` | `chunk()` | 248-249 |

