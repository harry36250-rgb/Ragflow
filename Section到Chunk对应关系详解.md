# Section 到 Chunk 对应关系详解

## 一、整体流程概览

```
Sections (来自 MinerU)
    ↓
Bboxes (一对一转换)
    ↓
图片上传到对象存储，替换为 img_id
    ↓
JSON 输出 (包含 text, position_tag, img_id)
    ↓
Splitter 提取 sections 和 images
    ↓
naive_merge_with_images 合并成 chunks
    ↓
最终 Chunks
```

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
- **图片裁剪**：`crop()` 方法根据位置标签从 PDF 页面裁剪出对应的图片区域
- **位置提取**：从位置标签字符串中解析出位置信息列表

**示例：**
```python
# 输入 section：
("安全须知", "@@1\t143.0\t540.0\t154.0\t279.0##")

# 转换后的 bbox：
{
    "text": "安全须知",
    "image": None,  # 如果位置标签对应的区域没有图片，则为 None
    "positions": [[1, 143.0, 540.0, 154.0, 279.0]]
}
```

---

## 三、Bbox → JSON 输出（图片上传）

### 3.1 图片上传位置

**文件：** `rag/flow/parser/parser.py`  
**方法：** `_invoke()` (第 814-817 行)

### 3.2 上传规则

```python
# 异步处理所有 JSON 输出中的图片
async with trio.open_nursery() as nursery:
    for d in outs.get("json", []):  # 遍历每个 bbox
        # image2id: 将 PIL.Image 对象转换为存储 ID
        # 如果 bbox 有图片，上传到对象存储（MinIO），替换为 img_id
        # 如果 bbox 没有图片，img_id 为 None
        nursery.start_soon(
            image2id,  # 上传图片并替换为 img_id
            d,  # bbox 字典
            partial(settings.STORAGE_IMPL.put, tenant_id=self._canvas._tenant_id),  # 上传函数
            get_uuid()  # 生成唯一 ID
        )
```

**转换结果：**
```python
# 上传前：
{
    "text": "图片描述",
    "image": PIL.Image,  # PIL.Image 对象
    "positions": [[4, 792.0, 967.0, 192.0, 342.0]]
}

# 上传后：
{
    "text": "图片描述",
    "img_id": "uuid-xxx-xxx",  # 图片在对象存储中的 ID
    "position_tag": "@@4\t792.0\t967.0\t192.0\t342.0##",  # 位置标签字符串
    "positions": [[4, 792.0, 967.0, 192.0, 342.0]]
}
```

**关键点：**
- **图片对象 → img_id**：PIL.Image 对象被上传到对象存储，替换为 `img_id`
- **位置标签生成**：从 `positions` 生成 `position_tag` 字符串
- **异步上传**：使用 `trio.open_nursery()` 并发上传多个图片

---

## 四、JSON → Sections + Images（Splitter 提取）

### 4.1 提取位置

**文件：** `rag/flow/splitter/splitter.py`  
**方法：** `_invoke()` (JSON 格式分支，第 111-115 行)

### 4.2 提取规则

```python
# 从 JSON 输出中提取 sections 和 images
sections, section_images = [], []
for o in from_upstream.json_result or []:  # 遍历每个 bbox
    # 提取文本和位置标签
    sections.append((
        o.get("text", ""),           # 文本内容
        o.get("position_tag", "")    # 位置标签字符串
    ))
    
    # 从对象存储获取图片（通过 img_id）
    section_images.append(
        id2image(  # 从对象存储下载图片，转换为 PIL.Image
            o.get("img_id"),  # 图片 ID
            partial(settings.STORAGE_IMPL.get, tenant_id=self._canvas._tenant_id)  # 下载函数
        )
    )
```

**关键点：**
- **一对一映射**：每个 bbox 对应一个 section 和一个 image
- **图片下载**：`id2image()` 从对象存储下载图片，转换为 PIL.Image 对象
- **格式统一**：sections 是 `(文本, 位置标签)` 元组列表

**示例：**
```python
# 输入 JSON：
[
    {"text": "安全须知", "position_tag": "@@1\t143.0\t540.0\t154.0\t279.0##", "img_id": None},
    {"text": "图片描述", "position_tag": "@@4\t792.0\t967.0\t192.0\t342.0##", "img_id": "uuid-xxx"},
]

# 提取后：
sections = [
    ("安全须知", "@@1\t143.0\t540.0\t154.0\t279.0##"),
    ("图片描述", "@@4\t792.0\t967.0\t192.0\t342.0##"),
]
section_images = [
    None,           # 文字 section，没有图片
    PIL.Image,      # 图片 section，从对象存储下载的图片
]
```

---

## 五、Sections + Images → Chunks（合并）

### 5.1 合并位置

**文件：** `rag/flow/splitter/splitter.py`  
**方法：** `_invoke()` (第 117-123 行)

### 5.2 合并规则

```python
# 调用 naive_merge_with_images 合并成 chunks
chunks, images = naive_merge_with_images(
    sections,           # [(文本, 位置标签), ...]
    section_images,     # [PIL.Image 或 None, ...]
    self._param.chunk_token_size,   # chunk 的最大 token 数
    deli,               # 分隔符
    self._param.overlapped_percent, # 重叠百分比
)
```

### 5.3 核心合并逻辑

**文件：** `rag/nlp/__init__.py`  
**方法：** `naive_merge_with_images()` (第 922-1050 行)

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
            # 图片垂直拼接
            result_images[-1] = concat_img(result_images[-1], image)
        tk_nums[-1] += tnum

# 遍历所有 sections
for text, image in zip(sections, section_images):
    add_chunk("\n" + text, image, position_tag)
```

**关键判断条件：**

```python
# 判断是否需要创建新 chunk
if cks[-1] == "" or tk_nums[-1] > chunk_token_num * (100 - overlapped_percent)/100.:
    # 创建新 chunk
else:
    # 合并到当前 chunk
```

**问题：** 当 `chunk_token_num = 0` 时：
- 条件变为：`tk_nums[-1] > 0`
- 如果当前 chunk 的 token 数 = 0，条件不满足
- 导致继续合并到当前 chunk

---

## 六、完整对应关系示例

### 6.1 输入：MinerU Sections

```python
sections = [
    ("安全须知", "@@1\t143.0\t540.0\t154.0\t279.0##"),
    ("图片1描述：黑色电子设备...", "@@4\t792.0\t967.0\t192.0\t342.0##"),
    ("严禁任何时候用双手同时触摸电池箱体的正负极柱。", "@@1\t100.0\t500.0\t400.0\t450.0##"),
    ("图片2描述：绿色证件...", "@@4\t334.0\t517.0\t438.0\t593.0##"),
]
```

### 6.2 第一步：Section → Bbox（一对一）

```python
bboxes = [
    {
        "text": "安全须知",
        "image": None,  # 文字 section，没有图片
        "positions": [[1, 143.0, 540.0, 154.0, 279.0]]
    },
    {
        "text": "图片1描述：黑色电子设备...",
        "image": PIL.Image1,  # 从 PDF 页面裁剪的图片
        "positions": [[4, 792.0, 967.0, 192.0, 342.0]]
    },
    {
        "text": "严禁任何时候用双手同时触摸电池箱体的正负极柱。",
        "image": None,
        "positions": [[1, 100.0, 500.0, 400.0, 450.0]]
    },
    {
        "text": "图片2描述：绿色证件...",
        "image": PIL.Image2,
        "positions": [[4, 334.0, 517.0, 438.0, 593.0]]
    },
]
```

### 6.3 第二步：图片上传，生成 JSON

```python
json_result = [
    {
        "text": "安全须知",
        "img_id": None,
        "position_tag": "@@1\t143.0\t540.0\t154.0\t279.0##",
        "positions": [[1, 143.0, 540.0, 154.0, 279.0]]
    },
    {
        "text": "图片1描述：黑色电子设备...",
        "img_id": "uuid-img1-xxx",  # 上传到对象存储后的 ID
        "position_tag": "@@4\t792.0\t967.0\t192.0\t342.0##",
        "positions": [[4, 792.0, 967.0, 192.0, 342.0]]
    },
    {
        "text": "严禁任何时候用双手同时触摸电池箱体的正负极柱。",
        "img_id": None,
        "position_tag": "@@1\t100.0\t500.0\t400.0\t450.0##",
        "positions": [[1, 100.0, 500.0, 400.0, 450.0]]
    },
    {
        "text": "图片2描述：绿色证件...",
        "img_id": "uuid-img2-xxx",
        "position_tag": "@@4\t334.0\t517.0\t438.0\t593.0##",
        "positions": [[4, 334.0, 517.0, 438.0, 593.0]]
    },
]
```

### 6.4 第三步：Splitter 提取

```python
sections = [
    ("安全须知", "@@1\t143.0\t540.0\t154.0\t279.0##"),
    ("图片1描述：黑色电子设备...", "@@4\t792.0\t967.0\t192.0\t342.0##"),
    ("严禁任何时候用双手同时触摸电池箱体的正负极柱。", "@@1\t100.0\t500.0\t400.0\t450.0##"),
    ("图片2描述：绿色证件...", "@@4\t334.0\t517.0\t438.0\t593.0##"),
]

section_images = [
    None,           # 从对象存储下载，如果没有 img_id 则为 None
    PIL.Image1,     # 从对象存储下载的图片
    None,
    PIL.Image2,
]
```

### 6.5 第四步：合并成 Chunks（假设 chunk_token_num = 512）

```python
# 第1个 section："安全须知"（约 10 tokens）
# 当前 chunk 为空，创建新 chunk
chunks = ["安全须知"]
images = [None]
tk_nums = [10]

# 第2个 section："图片1描述..."（约 50 tokens）
# 当前 chunk token 数 = 10 < 512，合并
chunks = ["安全须知\n图片1描述：黑色电子设备..."]
images = [PIL.Image1]
tk_nums = [60]

# 第3个 section："严禁任何时候..."（约 30 tokens）
# 当前 chunk token 数 = 60 < 512，合并
chunks = ["安全须知\n图片1描述：黑色电子设备...\n严禁任何时候用双手同时触摸电池箱体的正负极柱。"]
images = [PIL.Image1]
tk_nums = [90]

# 第4个 section："图片2描述..."（约 50 tokens）
# 当前 chunk token 数 = 90 < 512，合并
chunks = ["安全须知\n图片1描述：黑色电子设备...\n严禁任何时候用双手同时触摸电池箱体的正负极柱。\n图片2描述：绿色证件..."]
images = [concat_img(PIL.Image1, PIL.Image2)]  # 图片垂直拼接
tk_nums = [140]
```

### 6.6 第五步：生成最终 Chunks

```python
cks = [
    {
        "text": "安全须知\n图片1描述：黑色电子设备...\n严禁任何时候用双手同时触摸电池箱体的正负极柱。\n图片2描述：绿色证件...",
        "image": concat_img(PIL.Image1, PIL.Image2),  # 两个图片垂直拼接
        "positions": [
            [1, 143.0, 540.0, 154.0, 279.0],      # 安全须知的位置
            [4, 792.0, 967.0, 192.0, 342.0],      # 图片1的位置
            [1, 100.0, 500.0, 400.0, 450.0],      # 文字段落的位置
            [4, 334.0, 517.0, 438.0, 593.0],      # 图片2的位置
        ]
    }
]
```

---

## 七、对应关系总结

### 7.1 映射关系

| 阶段 | 输入 | 输出 | 映射关系 |
|------|------|------|----------|
| Section → Bbox | `(文本, 位置标签)` | `{text, image, positions}` | **一对一** |
| Bbox → JSON | `{text, image, positions}` | `{text, img_id, position_tag, positions}` | **一对一** |
| JSON → Sections | `{text, img_id, position_tag}` | `(文本, 位置标签)` + `PIL.Image` | **一对一** |
| Sections → Chunks | `[(文本, 位置标签), ...]` + `[图片, ...]` | `[{text, image, positions}, ...]` | **多对一** |

### 7.2 关键转换点

1. **Section → Bbox（一对一）**
   - 位置：`rag/flow/parser/parser.py:294-302`
   - 规则：每个 section 对应一个 bbox
   - 图片：根据位置标签从 PDF 页面裁剪

2. **图片上传（一对一）**
   - 位置：`rag/flow/parser/parser.py:814-817`
   - 规则：PIL.Image 对象上传到对象存储，替换为 `img_id`

3. **JSON → Sections（一对一）**
   - 位置：`rag/flow/splitter/splitter.py:111-115`
   - 规则：每个 bbox 对应一个 section 和一个 image

4. **Sections → Chunks（多对一）**
   - 位置：`rag/nlp/__init__.py:922-1050`
   - 规则：按 `chunk_token_num` 限制合并多个 sections
   - **这是真正合并的地方！**

### 7.3 合并判断条件

```python
# 判断是否需要创建新 chunk
if 当前chunk为空 or 当前chunk的token数 > chunk_token_num * (100 - overlapped_percent)/100:
    创建新 chunk
else:
    合并到当前 chunk
```

**当 `chunk_token_num = 0` 时：**
- 条件变为：`tk_nums[-1] > 0`
- 如果当前 chunk 的 token 数 = 0，条件不满足
- **导致继续合并，这是图片描述被合并的根本原因！**

---

## 八、关键代码位置

| 阶段 | 文件 | 方法/函数 | 行数 |
|------|------|----------|------|
| Section → Bbox | `rag/flow/parser/parser.py` | `_pdf()` (MinerU 分支) | 294-302 |
| 图片上传 | `rag/flow/parser/parser.py` | `_invoke()` | 814-817 |
| JSON → Sections | `rag/flow/splitter/splitter.py` | `_invoke()` | 111-115 |
| Sections → Chunks | `rag/nlp/__init__.py` | `naive_merge_with_images()` | 922-1050 |
| 最终 Chunks 生成 | `rag/flow/splitter/splitter.py` | `_invoke()` | 124-131 |

---

## 九、为什么是"多对一"？

### 9.1 原因

1. **Token 限制**：为了控制每个 chunk 的大小，需要将多个 sections 合并
2. **合并逻辑**：只要当前 chunk 的 token 数未达到上限，就会继续合并
3. **图片拼接**：多个图片会被垂直拼接成一个图片

### 9.2 示例

**输入 5 个 sections：**
```python
sections = [
    ("文字1", None),
    ("图片1描述", PIL.Image1),
    ("文字2", None),
    ("图片2描述", PIL.Image2),
    ("文字3", None),
]
```

**如果 `chunk_token_num = 512`，且每个 section 约 50 tokens：**
- 所有 5 个 sections 会被合并成 1 个 chunk
- 2 个图片会被垂直拼接成 1 个图片

**如果 `chunk_token_num = 100`：**
- 前 2 个 sections 合并成 chunk 1
- 后 3 个 sections 合并成 chunk 2

---

## 十、总结

### 10.1 对应关系

- **Section → Bbox**：**一对一**
- **Bbox → JSON**：**一对一**
- **JSON → Sections**：**一对一**
- **Sections → Chunks**：**多对一**（按 token 数限制合并）

### 10.2 合并规则

1. **判断条件**：`当前chunk的token数 > chunk_token_num * (100 - overlapped_percent)/100`
2. **文本合并**：直接追加，用 `\n` 分隔
3. **图片合并**：使用 `concat_img()` 垂直拼接
4. **位置合并**：所有 sections 的位置信息都会保留在 chunk 的 `positions` 列表中

### 10.3 问题根源

当 `chunk_token_num = 0` 时，判断条件 `tk_nums[-1] > 0` 可能不满足，导致所有 sections 被合并到一个 chunk，这就是图片描述被合并的根本原因。

