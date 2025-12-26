# 文字和图片 Section 合并规则详解

## 一、重要概念澄清

### 1.1 Section 本身不合并

**关键点：** Section 之间**不会直接合并**，它们始终保持一对一的关系。

- 每个 MinerU 输出块 → 一个 section
- 每个 section → 一个 bbox
- Section 到 bbox 的转换是**一对一**的

### 1.2 合并发生在两个阶段

1. **上下文添加阶段**（`attach_media_context`）：为图片/表格块添加上下文文本
2. **分块合并阶段**（`naive_merge_with_images`）：将多个 bboxes 合并成 chunks

---

## 二、Section → Bbox 转换（一对一）

### 2.1 转换位置

**文件：** `rag/flow/parser/parser.py`  
**方法：** `_pdf()` (MinerU 分支，第 294-302 行)

### 2.2 转换规则

```python
# MinerU 返回 lines: [(文本内容, 位置标签), ...]
lines, _ = pdf_parser.parse_pdf(...)

# 转换为 bboxes（一对一转换）
bboxes = []
for t, poss in lines:  # t: 文本内容, poss: 位置标签字符串
    box = {
        "image": pdf_parser.crop(poss, 1),  # 根据位置标签裁剪图片
        "positions": [[页码, x0, x1, top, bottom], ...],  # 提取位置信息
        "text": t,  # 文本内容
    }
    bboxes.append(box)  # 每个 section 对应一个 bbox
```

**关键点：**
- **一对一映射**：每个 section 对应一个 bbox
- **图片裁剪**：如果 section 有位置标签，会根据位置从 PDF 页面裁剪出对应的图片区域
- **文本保留**：section 的文本内容直接赋值给 bbox 的 `text` 字段

### 2.3 分类标记

```python
# 对每个 bbox 进行分类
for b in bboxes:
    text_val = b.get("text", "")
    has_text = isinstance(text_val, str) and text_val.strip()
    
    # 标记为图片块
    if layout == "figure" or (b.get("image") and not has_text):
        b["doc_type_kwd"] = "image"
    
    # 标记为表格块
    elif layout == "table":
        b["doc_type_kwd"] = "table"
```

**分类规则：**
- **图片块**：布局类型为 `figure`，或者有图片但没有文本
- **表格块**：布局类型为 `table`
- **文本块**：其他情况

---

## 三、上下文添加阶段（attach_media_context）

### 3.1 功能说明

**方法：** `attach_media_context()`  
**位置：** `rag/nlp/__init__.py:361-547`

**功能：** 为图片和表格块添加上下文文本（前后相邻的文本块）

**重要：** 这个阶段**不会合并 section**，只是修改每个 bbox 的内容，添加相邻的文本作为上下文。

### 3.2 触发条件

```python
# 在 parser.py 中调用
table_ctx = conf.get("table_context_size", 0) or 0  # 表格上下文大小（token 数）
image_ctx = conf.get("image_context_size", 0) or 0  # 图片上下文大小（token 数）

if table_ctx or image_ctx:
    bboxes = attach_media_context(bboxes, table_ctx, image_ctx)
```

**关键点：**
- 只有当 `table_context_size > 0` 或 `image_context_size > 0` 时才会执行
- 默认情况下这两个值都是 0，所以**通常不会执行**

### 3.3 合并规则（如果启用）

**规则：** 为每个图片/表格块添加**前后相邻的文本块**作为上下文

**流程：**

```python
# 1. 按位置排序（如果有位置信息）
# 排序规则：先按页码，再按 top（垂直位置），再按 left（水平位置）
positioned_indices.sort(key=lambda x: (页码, top, left, 原始索引))

# 2. 为每个图片/表格块添加上下文
for 当前块 in bboxes:
    if 当前块是图片块 and image_context_size > 0:
        # 向前查找文本块（最多 image_context_size 个 token）
        prev_ctx = []
        for 前一个块 in 前面的块列表:
            if 前一个块是文本块:  # 只从文本块中提取
                prev_ctx.append(前一个块的文本)
                # 如果超过 token 限制，截断
                if token数 > 剩余预算:
                    prev_ctx[-1] = 截断文本
                    break
        
        # 向后查找文本块（最多 image_context_size 个 token）
        next_ctx = []
        for 后一个块 in 后面的块列表:
            if 后一个块是文本块:  # 只从文本块中提取
                next_ctx.append(后一个块的文本)
                # 如果超过 token 限制，截断
                if token数 > 剩余预算:
                    next_ctx[-1] = 截断文本
                    break
        
        # 合并：前文 + 当前块文本 + 后文
        当前块["text"] = "\n".join([*prev_ctx, 当前块文本, *next_ctx])
```

**关键规则：**

1. **只添加文本块**：只从相邻的文本块中提取上下文，不会从其他图片/表格块中提取
2. **Token 限制**：上下文大小受 `image_context_size` 或 `table_context_size` 限制
3. **按位置排序**：如果有位置信息，会先按位置排序，确保上下文来自真正相邻的块
4. **不改变块数量**：只是修改每个块的内容，不会合并或拆分块
5. **遇到非文本块停止**：向前或向后查找时，遇到图片/表格块就停止

**示例：**

假设有以下 bboxes（按位置排序）：
```python
[
    {"text": "第一段文字", "doc_type_kwd": None},           # 文本块 1
    {"text": "第二段文字", "doc_type_kwd": None},           # 文本块 2
    {"text": "图片描述", "doc_type_kwd": "image", "image": PIL.Image},  # 图片块
    {"text": "第三段文字", "doc_type_kwd": None},           # 文本块 3
    {"text": "第四段文字", "doc_type_kwd": None},           # 文本块 4
]
```

如果 `image_context_size = 100`（假设每段文字约 50 tokens），执行后：

```python
# 图片块的内容变为：
{
    "text": "第二段文字\n图片描述\n第三段文字",  # 前文 + 当前 + 后文
    "doc_type_kwd": "image",
    "image": PIL.Image
}

# 其他块保持不变
```

---

## 四、分块合并阶段（naive_merge_with_images）

### 4.1 功能说明

**方法：** `naive_merge_with_images()`  
**位置：** `rag/nlp/__init__.py:846-921`

**功能：** 将多个 bboxes（sections）合并成 chunks

**这是真正合并的地方！**

### 4.2 调用位置

**文件：** `rag/flow/splitter/splitter.py`  
**方法：** `_invoke()` (JSON 格式分支，第 111-131 行)

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

### 4.3 核心合并规则

**判断条件：**

```python
def add_chunk(t, image, pos=""):
    tnum = num_tokens_from_string(t)  # 计算文本的 token 数
    
    # 判断是否需要创建新 chunk
    if 当前chunk为空 or 当前chunk的token数 > chunk_token_num * (100 - overlapped_percent)/100:
        # 创建新 chunk
        chunks.append(t)
        result_images.append(image)
    else:
        # 合并到当前 chunk
        chunks[-1] += t  # 文本追加
        if result_images[-1] is None:
            result_images[-1] = image
        else:
            # 图片垂直拼接
            result_images[-1] = concat_img(result_images[-1], image)
```

**合并规则：**

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

4. **位置标签处理：**
   - 位置标签会附加到文本末尾
   - 如果文本太短（< 8 tokens），不添加位置标签

### 4.4 MinerU 的特殊配置

**位置：** `rag/app/manual.py:248-249`

```python
if name in ["tcadp", "docling", "mineru"]:
    parser_config["chunk_token_num"] = 0
```

**问题：**

理论上 `chunk_token_num = 0` 应该让每个 section 独立成 chunk，但实际判断逻辑：

```python
if cks[-1] == "" or tk_nums[-1] > chunk_token_num * (100 - overlapped_percent)/100.:
```

当 `chunk_token_num = 0` 时：
- 条件变为：`tk_nums[-1] > 0`
- 如果当前 chunk 的 token 数 = 0，条件不满足
- 导致继续合并到当前 chunk

**这就是为什么图片描述会被合并的原因！**

---

## 五、完整合并流程示例

### 5.1 示例：多个图片和文字 section

**输入 sections（来自 MinerU）：**
```python
sections = [
    ("安全须知", "@@1\t143.0\t540.0\t154.0\t279.0##"),
    ("图片1描述：黑色电子设备...", "@@4\t792.0\t967.0\t192.0\t342.0##"),
    ("严禁任何时候用双手同时触摸电池箱体的正负极柱。", "@@1\t100.0\t500.0\t400.0\t450.0##"),
    ("图片2描述：绿色证件...", "@@4\t334.0\t517.0\t438.0\t593.0##"),
    ("要求维修人员必须持有...", "@@1\t200.0\t600.0\t500.0\t550.0##"),
]
```

**对应的 images：**
```python
section_images = [
    None,                    # 文字 section，没有图片
    PIL.Image1,             # 图片1
    None,                    # 文字 section，没有图片
    PIL.Image2,             # 图片2
    None,                    # 文字 section，没有图片
]
```

**合并过程（假设 `chunk_token_num = 512`）：**

```python
# 第1个 section："安全须知"（约 10 tokens）
# 当前 chunk 为空，创建新 chunk
chunks = ["安全须知"]
images = [None]
tk_nums = [10]

# 第2个 section："图片1描述..."（约 50 tokens）
# 当前 chunk token 数 = 10 < 512，合并
chunks = ["安全须知\n图片1描述：黑色电子设备..."]
images = [PIL.Image1]  # 直接赋值
tk_nums = [60]

# 第3个 section："严禁任何时候..."（约 30 tokens）
# 当前 chunk token 数 = 60 < 512，合并
chunks = ["安全须知\n图片1描述：黑色电子设备...\n严禁任何时候用双手同时触摸电池箱体的正负极柱。"]
images = [PIL.Image1]  # 保持不变
tk_nums = [90]

# 第4个 section："图片2描述..."（约 50 tokens）
# 当前 chunk token 数 = 90 < 512，合并
chunks = ["安全须知\n图片1描述：黑色电子设备...\n严禁任何时候用双手同时触摸电池箱体的正负极柱。\n图片2描述：绿色证件..."]
images = [concat_img(PIL.Image1, PIL.Image2)]  # 图片垂直拼接
tk_nums = [140]

# 第5个 section："要求维修人员..."（约 30 tokens）
# 当前 chunk token 数 = 140 < 512，合并
chunks = ["安全须知\n图片1描述：黑色电子设备...\n严禁任何时候用双手同时触摸电池箱体的正负极柱。\n图片2描述：绿色证件...\n要求维修人员必须持有..."]
images = [concat_img(PIL.Image1, PIL.Image2)]  # 保持不变
tk_nums = [170]
```

**最终结果：**
```python
chunks = [
    "安全须知\n图片1描述：黑色电子设备...\n严禁任何时候用双手同时触摸电池箱体的正负极柱。\n图片2描述：绿色证件...\n要求维修人员必须持有..."
]
images = [
    concat_img(PIL.Image1, PIL.Image2)  # 两个图片垂直拼接
]
```

**这就是为什么所有图片描述都被合并到一个 chunk 的原因！**

---

## 六、合并规则总结

### 6.1 Section 到 Bbox（一对一）

- **规则：** 每个 section 对应一个 bbox
- **不合并：** Section 之间不会合并
- **图片裁剪：** 根据位置标签从 PDF 页面裁剪图片

### 6.2 上下文添加（可选，默认不执行）

- **规则：** 为图片/表格块添加前后相邻的文本块作为上下文
- **限制：** 只从文本块中提取，受 token 数限制
- **不合并：** 只是修改内容，不改变块数量
- **默认：** `image_context_size = 0`，通常不执行

### 6.3 分块合并（真正合并的地方）

- **规则：** 按 `chunk_token_num` 限制合并多个 bboxes
- **判断条件：** `当前chunk的token数 > chunk_token_num * (100 - overlapped_percent)/100`
- **文本合并：** 直接追加，用 `\n` 分隔
- **图片合并：** 使用 `concat_img()` 垂直拼接
- **问题：** 即使 `chunk_token_num = 0`，判断条件可能不满足，导致继续合并

### 6.4 关键代码位置

| 阶段 | 文件 | 方法/函数 | 行数 |
|------|------|----------|------|
| Section → Bbox | `rag/flow/parser/parser.py` | `_pdf()` (MinerU 分支) | 294-302 |
| 上下文添加 | `rag/nlp/__init__.py` | `attach_media_context()` | 361-547 |
| 分块合并 | `rag/nlp/__init__.py` | `naive_merge_with_images()` | 846-921 |
| 调用合并 | `rag/flow/splitter/splitter.py` | `_invoke()` | 111-131 |

---

## 七、为什么图片描述会合并？

### 7.1 根本原因

1. **`naive_merge_with_images` 的合并逻辑：**
   - 只要当前 chunk 的 token 数未达到上限，就会继续合并
   - 即使 `chunk_token_num = 0`，判断条件 `tk_nums[-1] > 0` 可能不满足
   - 导致所有 sections 被合并到一个 chunk

2. **图片合并机制：**
   - `concat_img()` 会将多个图片垂直拼接
   - 所有图片的文本描述也会合并在一起

3. **没有强制分割：**
   - 即使每个图片有独立的 section，也没有机制强制每个 section 独立成 chunk

### 7.2 解决方案

如果需要每个图片独立成 chunk，可以：

1. **修改 `naive_merge_with_images`：**
   - 检测到图片块时，强制创建新 chunk
   - 或者检测到 `doc_type_kwd == "image"` 时，不合并

2. **修改判断条件：**
   - 当 `chunk_token_num = 0` 时，强制每个 section 独立成 chunk

3. **修改 `splitter.py`：**
   - 在调用 `naive_merge_with_images` 之前，先按类型分组
   - 图片块单独处理，不参与合并

