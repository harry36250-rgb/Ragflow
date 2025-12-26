# MinerU 类型转化详细流程（TEXT、TABLE、IMAGE）

一、整体流程概览

```
用户上传文件
    ↓
文件存储 + 创建文档记录
    ↓
创建解析任务（Task）→ Redis 队列
    ↓
Task Executor 拉取任务 → 启动 Pipeline
    ↓
Parser 组件 → MinerU 解析 PDF
    ↓
MinerU 输出 (outputs)
    ↓
Section (文本, 位置标签)
    ↓
Bbox {text, image, positions}
    ↓
JSON {text, img_id, position_tag, positions}
    ↓
Splitter 组件 → Section (文本, 位置标签) + Image (PIL.Image)
    ↓
Chunk {text, image, positions}
    ↓
Embedding 生成 → 向量库存储（Elasticsearch/Infinity）
    ↓
用户搜索/提问 → 向量检索 → API 返回
    ↓
前端展示（搜索列表 / 对话引用）
```

---

## 二、文件上传与任务调度（流程起点）

### 阶段 0.1：文件上传入口

**位置：** `api/apps/document_app.py:upload()`

**流程：**
```python
@manager.route('/upload', methods=['POST'])
async def upload():
    # 1. 接收前端上传的文件（form-data）
    # 2. 调用 FileService.upload_document() 保存到对象存储/本地
    # 3. 在数据库中创建 Document 记录（document 表）
    # 4. 返回文件/文档信息（doc_id、文件名等）
```

**关键代码位置：**
- **文件上传处理**：`api/apps/document_app.py:upload()`
- **文件存储服务**：`api/db/services/file_service.py:upload_document()`
- **文档记录创建**：`api/db/services/document_service.py`

上传完成后，只是**把文件和文档元信息存起来**，真正的解析要等下面的"任务调度"。

---

### 阶段 0.2：解析任务入队（Task）

**位置：** `api/db/services/task_service.py:queue_tasks()`

**流程：**

1. **根据知识库配置，生成解析 & 切分配置**：
```python
# api/db/services/document_service.py:get_chunking_config()
chunking_config = DocumentService.get_chunking_config(doc["id"])
# 返回配置包括：
# - parser_id: "mineru"  # 指定使用 MinerU 解析器
# - parser_config: {...}  # MinerU 相关参数（chunk 大小、是否启用 RAPTOR/GraphRAG 等）
```

2. **创建解析任务，写入 Task 表并推送到 Redis 队列**：
```python
# api/db/services/task_service.py:queue_tasks()
def queue_tasks(doc: dict, bucket: str, name: str, priority: int):
    # 1. 根据 doc_id 生成 chunking_config（包括 parser_id=mineru 等）
    chunking_config = DocumentService.get_chunking_config(doc["id"])
    # 2. 生成一个或多个解析任务（支持分页解析）
    # 3. 计算 task.digest，用于去重/复用
    # 4. 把任务写入 Task 表，并 push 到 Redis 队列
    REDIS_CONN.queue_product(
        settings.get_svr_queue_name(priority), 
        message=unfinished_task
    )
```

**关键代码位置：**
- **任务创建**：`api/db/services/task_service.py:queue_tasks()`
- **配置生成**：`api/db/services/document_service.py:get_chunking_config()`
- **任务入队**：通过 Redis 队列传递任务消息

从这一刻开始，MinerU + RAGFlow 的 **数据流** 就交给后台的 `task_executor` 处理。

---

### 阶段 0.3：Task Executor 拉取任务并启动 Pipeline

**位置：** `rag/svr/task_executor.py:run_dataflow()` / `do_handle_task()`

**流程：**

```python
# rag/svr/task_executor.py:run_dataflow()
async def run_dataflow(task: dict):
    # 1. 根据 DSL 创建 Pipeline（其中包含 Parser、Splitter 等组件）
    pipeline = Pipeline(
        dsl, 
        tenant_id=task_tenant_id, 
        doc_id=task_doc_id, 
        task_id=task_id, 
        flow_id=task["flow_id"]
    )
    # 2. 运行 Pipeline：内部会按顺序调用 Parser → Splitter → 其他组件
    chunks = await pipeline.run(file=task["file"])
```

**Pipeline 执行流程：**
1. **Parser 组件**：根据 `parse_method == "mineru"` 调用 MinerU 解析器
2. **Splitter 组件**：将 Parser 输出的 bboxes 合并成 chunks
3. **Embedding 生成**：为 chunks 生成向量
4. **向量库存储**：将 chunks 插入到 Elasticsearch/Infinity

**关键代码位置：**
- **Pipeline 执行**：`rag/svr/task_executor.py:run_dataflow()`
- **标准流程**：`rag/svr/task_executor.py:do_handle_task()`
- **Pipeline 引擎**：`rag/flow/pipeline.py:Pipeline.run()`

对 MinerU 场景而言：
- `Pipeline` 内部会调用 `Parser` 组件  
- `Parser._pdf()` 分支根据 `parse_method == "mineru"` 走 MinerU 解析  
- 后续就进入下面详细写的：**MinerU → Section → Bbox → Chunk → 向量库**。

---

## 三、TEXT 类型转化流程

### 阶段 1：MinerU 输出 → Section

**位置：** `deepdoc/parser/mineru_parser.py:813-815`

**MinerU 输出格式：**
```python
output = {
    "type": "text",
    "text": "这是文本内容",
    "bbox": [x0, top, x1, bottom],
    "page_idx": 0,
    # ... 其他字段
}
```

**转换逻辑：**
```python
case MinerUContentType.TEXT:
    # 文本类型：直接使用文本内容
    section = output["text"]  # "这是文本内容"
```

**生成位置标签：**
```python
position_tag = self._line_tag(output)  # "@@1\t143.0\t540.0\t154.0\t279.0##"
```

**最终 Section：**
```python
section = ("这是文本内容", "@@1\t143.0\t540.0\t154.0\t279.0##")
```

---

### 阶段 2：Section → Bbox

**位置：** `rag/flow/parser/parser.py:296-302`

**输入：**
```python
t = "这是文本内容"
poss = "@@1\t143.0\t540.0\t154.0\t279.0##"
```

**转换逻辑：**
```python
box = {
    "image": pdf_parser.crop(poss, 1),  # 根据位置标签裁剪图片（TEXT 类型通常为 None）
    "positions": [[1, 143.0, 540.0, 154.0, 279.0]],  # 提取位置信息
    "text": "这是文本内容",  # 文本内容
}
```

**最终 Bbox：**
```python
{
    "text": "这是文本内容",
    "image": None,  # TEXT 类型通常没有图片
    "positions": [[1, 143.0, 540.0, 154.0, 279.0]],
    "doc_type_kwd": None,  # 不是图片也不是表格
}
```

---

### 阶段 3：Bbox → JSON（图片上传）

**位置：** `rag/flow/parser/parser.py:882-887`

**输入：**
```python
bbox = {
    "text": "这是文本内容",
    "image": None,
    "positions": [[1, 143.0, 540.0, 154.0, 279.0]],
}
```

**图片上传：**
```python
# image2id 处理：如果 image 为 None，不执行上传
# 最终 img_id = None
```

**生成 position_tag：**
```python
position_tag = "@@1\t143.0\t540.0\t154.0\t279.0##"
```

**最终 JSON：**
```python
{
    "text": "这是文本内容",
    "img_id": None,  # 没有图片
    "position_tag": "@@1\t143.0\t540.0\t154.0\t279.0##",
    "positions": [[1, 143.0, 540.0, 154.0, 279.0]],
}
```

---

### 阶段 4：JSON → Section（Splitter 提取）

**位置：** `rag/flow/splitter/splitter.py:111-115`

**输入：**
```python
json_result = {
    "text": "这是文本内容",
    "img_id": None,
    "position_tag": "@@1\t143.0\t540.0\t154.0\t279.0##",
}
```

**提取逻辑：**
```python
sections.append((
    "这是文本内容",  # text
    "@@1\t143.0\t540.0\t154.0\t279.0##"  # position_tag
))

section_images.append(
    id2image(None, ...)  # None → None
)
```

**最终 Sections：**
```python
sections = [("这是文本内容", "@@1\t143.0\t540.0\t154.0\t279.0##")]
section_images = [None]
```

---

### 阶段 5：Section → Chunk（合并）

**位置：** `rag/nlp/__init__.py:958-1029`

**输入：**
```python
text = "这是文本内容"
image = None
pos = "@@1\t143.0\t540.0\t154.0\t279.0##"
```

**合并逻辑：**
```python
# 计算 token 数
tnum = num_tokens_from_string("这是文本内容")  # 假设 = 10

# 判断是否需要创建新 chunk
if cks[-1] == "" or tk_nums[-1] > chunk_token_num * (100 - overlapped_percent)/100:
    # 创建新 chunk
    cks.append("\n这是文本内容@@1\t143.0\t540.0\t154.0\t279.0##")
    result_images.append(None)
    tk_nums.append(10)
else:
    # 合并到当前 chunk
    cks[-1] += "\n这是文本内容@@1\t143.0\t540.0\t154.0\t279.0##"
    # image 为 None，不处理
    tk_nums[-1] += 10
```

**最终 Chunk：**
```python
{
    "text": "这是文本内容",  # remove_tag 后
    "image": None,
    "positions": [[1, 143.0, 540.0, 154.0, 279.0]],
}
```

---

## 四、TABLE 类型转化流程

### 阶段 1：MinerU 输出 → Section

**位置：** `deepdoc/parser/mineru_parser.py:816-820`

**MinerU 输出格式：**
```python
output = {
    "type": "table",
    "table_body": "| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |",
    "table_caption": ["表格标题"],
    "table_footnote": ["表格脚注"],
    "bbox": [x0, top, x1, bottom],
    "page_idx": 1,
    # ... 其他字段
}
```

**转换逻辑：**
```python
case MinerUContentType.TABLE:
    # 表格类型：组合表格主体、标题和脚注
    section = (
        output.get("table_body", "") +  # "| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |"
        "\n".join(output.get("table_caption", [])) +  # "表格标题"
        "\n".join(output.get("table_footnote", []))  # "表格脚注"
    )
    if not section.strip():
        section = "FAILED TO PARSE TABLE"  # 如果解析失败，使用占位文本
```

**生成位置标签：**
```python
position_tag = self._line_tag(output)  # "@@2\t100.0\t500.0\t200.0\t400.0##"
```

**最终 Section：**
```python
section = (
    "| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注",
    "@@2\t100.0\t500.0\t200.0\t400.0##"
)
```

---

### 阶段 2：Section → Bbox

**位置：** `rag/flow/parser/parser.py:296-302`

**输入：**
```python
t = "| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注"
poss = "@@2\t100.0\t500.0\t200.0\t400.0##"
```

**转换逻辑：**
```python
box = {
    "image": pdf_parser.crop(poss, 1),  # 根据位置标签裁剪图片（TABLE 类型可能为 None）
    "positions": [[2, 100.0, 500.0, 200.0, 400.0]],
    "text": "| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注",
}
```

**分类标记：**
```python
# 在 parser.py:361-371 中
layout = b.get("layout_type")  # 可能为 "table"
if layout == "table":
    b["doc_type_kwd"] = "table"  # 标记为表格块
```

**最终 Bbox：**
```python
{
    "text": "| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注",
    "image": None,  # TABLE 类型通常没有图片
    "positions": [[2, 100.0, 500.0, 200.0, 400.0]],
    "doc_type_kwd": "table",  # 标记为表格
}
```

---

### 阶段 3：Bbox → JSON（图片上传）

**位置：** `rag/flow/parser/parser.py:882-887`

**输入：**
```python
bbox = {
    "text": "| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注",
    "image": None,
    "positions": [[2, 100.0, 500.0, 200.0, 400.0]],
    "doc_type_kwd": "table",
}
```

**图片上传：**
```python
# image2id 处理：如果 image 为 None，不执行上传
# 最终 img_id = None
```

**最终 JSON：**
```python
{
    "text": "| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注",
    "img_id": None,
    "position_tag": "@@2\t100.0\t500.0\t200.0\t400.0##",
    "positions": [[2, 100.0, 500.0, 200.0, 400.0]],
    "doc_type_kwd": "table",
}
```

---

### 阶段 4：JSON → Section（Splitter 提取）

**位置：** `rag/flow/splitter/splitter.py:111-115`

**输入：**
```python
json_result = {
    "text": "| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注",
    "img_id": None,
    "position_tag": "@@2\t100.0\t500.0\t200.0\t400.0##",
}
```

**提取逻辑：**
```python
sections.append((
    "| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注",
    "@@2\t100.0\t500.0\t200.0\t400.0##"
))

section_images.append(None)
```

**最终 Sections：**
```python
sections = [("| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注", "@@2\t100.0\t500.0\t200.0\t400.0##")]
section_images = [None]
```

---

### 阶段 5：Section → Chunk（合并）

**位置：** `rag/nlp/__init__.py:958-1029`

**输入：**
```python
text = "| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注"
image = None
pos = "@@2\t100.0\t500.0\t200.0\t400.0##"
```

**合并逻辑：**
```python
# 计算 token 数
tnum = num_tokens_from_string(text)  # 假设 = 50

# 判断是否需要创建新 chunk
if cks[-1] == "" or tk_nums[-1] > chunk_token_num * (100 - overlapped_percent)/100:
    # 创建新 chunk
    cks.append("\n| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注@@2\t100.0\t500.0\t200.0\t400.0##")
    result_images.append(None)
    tk_nums.append(50)
else:
    # 合并到当前 chunk
    cks[-1] += "\n| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注@@2\t100.0\t500.0\t200.0\t400.0##"
    tk_nums[-1] += 50
```

**最终 Chunk：**
```python
{
    "text": "| 列1 | 列2 |\n| --- | --- |\n| 值1 | 值2 |\n表格标题\n表格脚注",  # remove_tag 后
    "image": None,
    "positions": [[2, 100.0, 500.0, 200.0, 400.0]],
}
```

---

## 五、IMAGE 类型转化流程

### 阶段 1：MinerU 输出 → Section

**位置：** `deepdoc/parser/mineru_parser.py:821-831`

**MinerU 输出格式：**
```python
output = {
    "type": "image",
    "img_path": "/path/to/image.jpg",  # 图片文件路径
    "image_caption": ["图片标题：黑色电子设备"],  # 图片标题列表
    "image_footnote": ["图片脚注"],  # 图片脚注列表
    "bbox": [x0, top, x1, bottom],
    "page_idx": 3,
    # ... 其他字段
}
```

**转换逻辑：**
```python
case MinerUContentType.IMAGE:
    # 图片类型：组合图片标题和脚注
    image_path = output.get("img_path", "")
    
    # 如果 image_caption 为空，可以调用 Dify 生成（当前代码中已注释）
    # processed_result = self._process_image_path(image_path) if image_path else ""
    # caption = "".join(output.get("image_caption", [])) or processed_result
    
    # 当前实现：直接使用 image_caption
    section = (
        "".join(output.get("image_caption", [])) +  # "图片标题：黑色电子设备"
        "\n" +
        "".join(output.get("image_footnote", []))  # "图片脚注"
    )
```

**生成位置标签：**
```python
position_tag = self._line_tag(output)  # "@@4\t792.0\t967.0\t192.0\t342.0##"
```

**最终 Section：**
```python
section = (
    "图片标题：黑色电子设备\n图片脚注",
    "@@4\t792.0\t967.0\t192.0\t342.0##"
)
```

---

### 阶段 2：Section → Bbox

**位置：** `rag/flow/parser/parser.py:296-302`

**输入：**
```python
t = "图片标题：黑色电子设备\n图片脚注"
poss = "@@4\t792.0\t967.0\t192.0\t342.0##"
```

**转换逻辑：**
```python
box = {
    "image": pdf_parser.crop(poss, 1),  # 根据位置标签裁剪图片（IMAGE 类型有图片）
    "positions": [[4, 792.0, 967.0, 192.0, 342.0]],
    "text": "图片标题：黑色电子设备\n图片脚注",
}
```

**分类标记：**
```python
# 在 parser.py:361-371 中
text_val = "图片标题：黑色电子设备\n图片脚注"
has_text = True  # 有文本内容
layout = b.get("layout_type")  # 可能为 "figure"

# 判断逻辑
if layout == "figure" or (b.get("image") and not has_text):
    b["doc_type_kwd"] = "image"  # 标记为图片块
# 注意：如果 has_text = True，可能不会被标记为 "image"
```

**最终 Bbox：**
```python
{
    "text": "图片标题：黑色电子设备\n图片脚注",
    "image": PIL.Image,  # 从 PDF 页面裁剪的图片对象
    "positions": [[4, 792.0, 967.0, 192.0, 342.0]],
    "doc_type_kwd": "image",  # 如果 layout == "figure" 或 (有图片且无文本)
}
```

---

### 阶段 3：Bbox → JSON（图片上传）

**位置：** `rag/flow/parser/parser.py:882-887`

**输入：**
```python
bbox = {
    "text": "图片标题：黑色电子设备\n图片脚注",
    "image": PIL.Image,  # PIL.Image 对象
    "positions": [[4, 792.0, 967.0, 192.0, 342.0]],
    "doc_type_kwd": "image",
}
```

**图片上传：**
```python
# image2id 处理：
# 1. 将 PIL.Image 转换为 JPEG 格式的字节流
# 2. 上传到对象存储（MinIO/S3）
# 3. 生成唯一 ID（UUID）
# 4. 替换 bbox["image"] 为 img_id
```

**生成 position_tag：**
```python
position_tag = "@@4\t792.0\t967.0\t192.0\t342.0##"
```

**最终 JSON：**
```python
{
    "text": "图片标题：黑色电子设备\n图片脚注",
    "img_id": "uuid-img-xxx-xxx",  # 上传后的图片 ID
    "position_tag": "@@4\t792.0\t967.0\t192.0\t342.0##",
    "positions": [[4, 792.0, 967.0, 192.0, 342.0]],
    "doc_type_kwd": "image",
}
```

---

### 阶段 4：JSON → Section（Splitter 提取）

**位置：** `rag/flow/splitter/splitter.py:111-115`

**输入：**
```python
json_result = {
    "text": "图片标题：黑色电子设备\n图片脚注",
    "img_id": "uuid-img-xxx-xxx",
    "position_tag": "@@4\t792.0\t967.0\t192.0\t342.0##",
}
```

**提取逻辑：**
```python
sections.append((
    "图片标题：黑色电子设备\n图片脚注",
    "@@4\t792.0\t967.0\t192.0\t342.0##"
))

section_images.append(
    id2image("uuid-img-xxx-xxx", ...)  # 从对象存储下载，转换为 PIL.Image
)
```

**最终 Sections：**
```python
sections = [("图片标题：黑色电子设备\n图片脚注", "@@4\t792.0\t967.0\t192.0\t342.0##")]
section_images = [PIL.Image]  # 从对象存储下载的图片对象
```

---

### 阶段 5：Section → Chunk（合并）

**位置：** `rag/nlp/__init__.py:958-1029`

**输入：**
```python
text = "图片标题：黑色电子设备\n图片脚注"
image = PIL.Image  # 图片对象
pos = "@@4\t792.0\t967.0\t192.0\t342.0##"
```

**合并逻辑：**
```python
# 计算 token 数
tnum = num_tokens_from_string("图片标题：黑色电子设备\n图片脚注")  # 假设 = 30

# 判断是否需要创建新 chunk
if cks[-1] == "" or tk_nums[-1] > chunk_token_num * (100 - overlapped_percent)/100:
    # 创建新 chunk
    cks.append("\n图片标题：黑色电子设备\n图片脚注@@4\t792.0\t967.0\t192.0\t342.0##")
    result_images.append(PIL.Image)  # 直接赋值
    tk_nums.append(30)
else:
    # 合并到当前 chunk
    cks[-1] += "\n图片标题：黑色电子设备\n图片脚注@@4\t792.0\t967.0\t192.0\t342.0##"
    
    # 图片处理
    if result_images[-1] is None:
        result_images[-1] = PIL.Image  # 如果当前 chunk 没有图片，直接赋值
    else:
        result_images[-1] = concat_img(result_images[-1], PIL.Image)  # 垂直拼接
    
    tk_nums[-1] += 30
```

**最终 Chunk：**
```python
{
    "text": "图片标题：黑色电子设备\n图片脚注",  # remove_tag 后
    "image": PIL.Image,  # 图片对象（可能是拼接后的）
    "positions": [[4, 792.0, 967.0, 192.0, 342.0]],
}
```

---

## 六、三种类型对比总结

| 类型 | Section 文本来源 | Bbox image | JSON img_id | Chunk image |
|------|----------------|------------|-------------|-------------|
| **TEXT** | `output["text"]` | `None` | `None` | `None` |
| **TABLE** | `table_body + table_caption + table_footnote` | `None` | `None` | `None` |
| **IMAGE** | `image_caption + image_footnote` | `PIL.Image`（裁剪） | `uuid-xxx`（上传后） | `PIL.Image`（下载后，可能拼接） |

---

## 七、关键转换点

### 1. Section 文本生成

- **TEXT**：直接使用 `output["text"]`
- **TABLE**：组合 `table_body + table_caption + table_footnote`
- **IMAGE**：组合 `image_caption + image_footnote`

### 2. 图片处理

- **TEXT/TABLE**：通常没有图片，`image = None`
- **IMAGE**：
  - 阶段 2：从 PDF 页面裁剪（`pdf_parser.crop()`）
  - 阶段 3：上传到对象存储，替换为 `img_id`
  - 阶段 4：从对象存储下载，恢复为 `PIL.Image`
  - 阶段 5：可能与其他图片垂直拼接（`concat_img()`）

### 3. 分类标记

- **TEXT**：`doc_type_kwd = None`
- **TABLE**：`doc_type_kwd = "table"`（如果 `layout == "table"`）
- **IMAGE**：`doc_type_kwd = "image"`（如果 `layout == "figure"` 或 `(有图片且无文本)`）

### 4. 合并规则

- **判断条件**：`当前chunk为空 or 当前chunk的token数 > chunk_token_num * (100 - overlapped_percent)/100`
- **文本合并**：直接追加，用 `\n` 分隔
- **图片合并**：使用 `concat_img()` 垂直拼接（仅 IMAGE 类型）

---

## 八、完整示例

### 示例：包含 TEXT、TABLE、IMAGE 的文档

**MinerU 输出：**
```python
outputs = [
    {"type": "text", "text": "第一段文字", ...},
    {"type": "image", "image_caption": ["图片1"], "img_path": "/path/to/img1.jpg", ...},
    {"type": "table", "table_body": "| 列1 | 列2 |", "table_caption": ["表格1"], ...},
    {"type": "text", "text": "第二段文字", ...},
    {"type": "image", "image_caption": ["图片2"], "img_path": "/path/to/img2.jpg", ...},
]
```

**最终 Chunks（假设 `chunk_token_num = 512`，所有内容合并到一个 chunk）：**
```python
chunks = [
    {
        "text": "第一段文字\n图片1\n| 列1 | 列2 |\n表格1\n第二段文字\n图片2",
        "image": concat_img(PIL.Image1, PIL.Image2),  # 两个图片垂直拼接
        "positions": [
            [1, ...],  # 第一段文字
            [2, ...],  # 图片1
            [3, ...],  # 表格1
            [4, ...],  # 第二段文字
            [5, ...],  # 图片2
        ]
    }
]
```

---

## 九、Chunk 存储到向量库流程

### 阶段 6：Chunk → 向量库存储

**位置：** `rag/svr/task_executor.py:720-773` (`insert_es` 函数)

**输入：**
```python
chunks = [
    {
        "id": "chunk-uuid-xxx",
        "text": "图片标题：黑色电子设备\n图片脚注",
        "img_id": "imagetemps-uuid-xxx",
        "positions": [[4, 792.0, 967.0, 192.0, 342.0]],
        "doc_id": "doc-xxx",
        "kb_id": ["kb-xxx"],
        "content_with_weight": "图片标题：黑色电子设备\n图片脚注",
        "content_ltks": "图片标题：黑色电子设备\n图片脚注",  # 用于高亮
        "doc_type_kwd": "image",
        # ... 其他字段
    },
    # ... 更多 chunks
]
```

**存储流程：**

1. **处理母块（Mother Chunks）**（如果有）：
```python
# 如果 chunk 有 "mom" 字段（父级块），先存储母块
for ck in chunks:
    mom = ck.get("mom") or ck.get("mom_with_weight") or ""
    if mom:
        mom_ck = {
            "id": mom_id,  # 基于 mom 内容生成的 hash
            "content_with_weight": mom,
            "available_int": 0,  # 母块不可用（仅用于层级关系）
            # ... 其他字段
        }
        mothers.append(mom_ck)

# 批量插入母块
settings.docStoreConn.insert(mothers, index_name, dataset_id)
```

2. **生成 Embedding 向量**：
```python
# 在 do_handle_task() 中（第 936 行）
token_count, vector_size = await embedding(chunks, embedding_model, parser_config, callback)
# 为每个 chunk 生成向量，添加到 chunks 中：
# chunks[i]["q_768_vec"] = [0.1, 0.2, ...]  # 768 维向量（取决于模型）
```

3. **批量插入 Chunks**：
```python
# 分批插入（每批 DOC_BULK_SIZE 个）
for b in range(0, len(chunks), settings.DOC_BULK_SIZE):
    # 插入到 Elasticsearch/Infinity
    doc_store_result = await trio.to_thread.run_sync(
        lambda: settings.docStoreConn.insert(
            chunks[b:b + settings.DOC_BULK_SIZE],
            search.index_name(task_tenant_id),
            task_dataset_id
        )
    )
    
    # 更新任务中的 chunk_ids
    chunk_ids = [chunk["id"] for chunk in chunks[:b + settings.DOC_BULK_SIZE]]
    TaskService.update_chunk_ids(task_id, " ".join(chunk_ids))
```

**存储的 Chunk 数据结构：**
```python
{
    "id": "chunk-uuid-xxx",  # Chunk 唯一 ID
    "content_with_weight": "图片标题：黑色电子设备\n图片脚注",  # 文本内容
    "content_ltks": "图片标题：黑色电子设备\n图片脚注",  # 用于高亮搜索
    "img_id": "imagetemps-uuid-xxx",  # 图片 ID（如果有）
    "position_int": [[4, 792.0, 967.0, 192.0, 342.0]],  # 位置信息
    "doc_id": "doc-xxx",  # 文档 ID
    "kb_id": ["kb-xxx"],  # 知识库 ID
    "docnm_kwd": "文档名称",  # 文档名称
    "doc_type_kwd": "image",  # 文档类型（image/table/text）
    "q_768_vec": [0.1, 0.2, ...],  # 768 维向量（用于相似度搜索）
    "available_int": 1,  # 是否可用（1=可用，0=不可用）
    "create_time": "2025-01-01 12:00:00",  # 创建时间
    # ... 其他字段
}
```

**关键代码位置：**
- **存储函数**：`rag/svr/task_executor.py:720-773` (`insert_es`)
- **Embedding 生成**：`rag/svr/task_executor.py:936` (`embedding`)
- **任务处理**：`rag/svr/task_executor.py:777-964` (`do_handle_task`)

---

## 十、Chunk 检索流程

### 阶段 7：向量库检索 → API 返回

**位置：** `rag/nlp/search.py:359-480` (`retrieval` 方法)

**检索流程：**

1. **向量相似度搜索**：
```python
# 使用用户问题生成向量
question_vector = embd_mdl.encode([question])[0]

# 在向量库中搜索相似 chunks
sres = self.dataStore.search(
    query_vector=question_vector,
    top_k=top,
    filters={"doc_ids": doc_ids, "kb_ids": kb_ids},
    index_name=index_name
)
# sres.ids: 匹配的 chunk IDs
# sres.field: {chunk_id: chunk_data, ...}
```

2. **计算相似度分数**：
```python
# 向量相似度
vector_similarity = cosine_similarity(question_vector, chunk_vector)

# 文本相似度（BM25）
term_similarity = self.qryr.score(question, chunk["content_ltks"])

# 混合相似度
similarity = vector_weight * vector_similarity + (1 - vector_weight) * term_similarity
```

3. **构建返回结果**：
```python
# rag/nlp/search.py:450-478
for i in page_idx:
    id = sres.ids[i]
    chunk = sres.field[id]
    
    d = {
        "chunk_id": id,
        "content_ltks": chunk["content_ltks"],  # 用于高亮
        "content_with_weight": chunk["content_with_weight"],  # 完整内容
        "doc_id": chunk["doc_id"],
        "docnm_kwd": chunk["docnm_kwd"],
        "kb_id": chunk["kb_id"],
        "image_id": chunk.get("img_id", ""),  # 图片 ID
        "similarity": float(sim_np[i]),  # 相似度分数
        "vector_similarity": float(vsim[i]),
        "term_similarity": float(tsim[i]),
        "positions": chunk.get("position_int", []),  # 位置信息
        "doc_type_kwd": chunk.get("doc_type_kwd", ""),  # image/table/text
    }
    
    # 高亮处理（如果启用）
    if highlight and id in sres.highlight:
        d["highlight"] = remove_redundant_spaces(sres.highlight[id])
    
    ranks["chunks"].append(d)
```

**API 返回格式：**
```python
# api/apps/chunk_app.py:64-82
{
    "total": 100,  # 总匹配数
    "chunks": [
        {
            "chunk_id": "chunk-uuid-xxx",
            "content_with_weight": "图片标题：黑色电子设备\n图片脚注",
            "highlight": "<mark>图片标题</mark>：黑色电子设备\n图片脚注",  # 高亮后的文本
            "doc_id": "doc-xxx",
            "docnm_kwd": "文档名称",
            "image_id": "imagetemps-uuid-xxx",  # 图片 ID
            "positions": [[4, 792.0, 967.0, 192.0, 342.0]],  # 位置信息
            "available_int": 1,
            "important_kwd": [],
            "question_kwd": [],
        },
        # ... 更多 chunks
    ],
    "doc": {
        "id": "doc-xxx",
        "name": "文档名称",
        # ... 其他文档信息
    }
}
```

**关键代码位置：**
- **检索函数**：`rag/nlp/search.py:359-480` (`retrieval`)
- **API 端点**：`api/apps/chunk_app.py:41-87` (`list_chunk`)
- **SDK API**：`api/apps/sdk/doc.py:857-1022` (`list_chunks`)

---

## 十一、前端展示流程

### 阶段 8：API 返回 → 前端展示

**位置：** `web/src/pages/next-search/search-view.tsx:195-242`

**前端展示流程：**

1. **获取搜索结果**：
```typescript
// 调用 API 获取 chunks
const response = await fetch('/api/chunk/list', {
    method: 'POST',
    body: JSON.stringify({
        doc_id: documentId,
        page: 1,
        size: 30,
        keywords: searchStr
    })
});
const data = await response.json();
// data.chunks: Chunk 数组
```

2. **渲染 Chunk 列表**：
```typescript
// web/src/pages/next-search/search-view.tsx:199-242
{chunks.map((chunk, index) => {
    return (
        <div key={index}>
            <div className="w-full flex flex-col">
                {/* 图片展示 */}
                <div className="w-full highlightContent">
                    {/* ImageWithPopover: 如果 image_id 存在，显示图片 */}
                    <ImageWithPopover id={chunk.img_id}></ImageWithPopover>
                    
                    {/* 文本内容（可点击展开） */}
                    <Popover>
                        <PopoverTrigger asChild>
                            <div
                                dangerouslySetInnerHTML={{
                                    __html: DOMPurify.sanitize(
                                        `${chunk.highlight}...`
                                    ),
                                }}
                                className="text-sm text-text-primary mb-1"
                            ></div>
                        </PopoverTrigger>
                        <PopoverContent>
                            {/* 完整内容 */}
                            <HightLightMarkdown>
                                {chunk.content_with_weight}
                            </HightLightMarkdown>
                        </PopoverContent>
                    </Popover>
                </div>
                
                {/* 文档信息 */}
                <div
                    className="flex gap-2 items-center text-xs"
                    onClick={() => clickDocumentButton(chunk.doc_id, chunk)}
                >
                    <FileIcon name={chunk.docnm_kwd}></FileIcon>
                    {chunk.docnm_kwd}
                </div>
            </div>
        </div>
    );
})}
```

3. **图片组件实现**：
```typescript
// web/src/components/image/index.tsx:11-20
const Image = ({ id, className, ...props }: IImage) => {
    return (
        <img
            {...props}
            // 通过 API 获取图片：/api/document/image/{img_id}
            src={`${api_host}/document/image/${id}`}
            alt=""
            className={classNames('max-w-[45vw] max-h-[40wh] block', className)}
        />
    );
};

// ImageWithPopover: 鼠标悬停显示大图
export const ImageWithPopover = ({ id }: { id: string }) => {
    return (
        <Popover>
            <PopoverTrigger>
                <Image id={id} className="max-h-[100px] inline-block"></Image>
            </PopoverTrigger>
            <PopoverContent>
                <Image id={id} className="max-w-[100px] object-contain"></Image>
            </PopoverContent>
        </Popover>
    );
};
```

4. **Chunk 卡片组件**（文档详情页）：
```typescript
// web/src/pages/dataflow-result/components/chunk-card/index.tsx:74-99
{item.image_id && (
    <Popover open={open}>
        <PopoverTrigger
            asChild
            onMouseEnter={() => setOpen(true)}
            onMouseLeave={() => setOpen(false)}
        >
            <div>
                {/* 缩略图 */}
                <Image id={item.image_id} className={styles.image}></Image>
            </div>
        </PopoverTrigger>
        <PopoverContent>
            {/* 预览大图 */}
            <Image
                id={item.image_id}
                className={styles.imagePreview}
            ></Image>
        </PopoverContent>
    </Popover>
)}
```

5. **图片 API 端点**：
```python
# api/apps/chunk_app.py 或其他地方
@manager.route('/document/image/<image_id>', methods=['GET'])
def get_image(image_id):
    # 解析 image_id: "bucket-uuid"
    bucket, filename = image_id.split("-", 1)
    
    # 从对象存储获取图片
    image_data = settings.STORAGE_IMPL.get(bucket=bucket, filename=filename)
    
    # 返回图片数据
    return Response(image_data, mimetype='image/jpeg')
```

**前端识别 Chunk 的关键字段：**

| 字段 | 说明 | 用途 |
|------|------|------|
| `chunk.img_id` 或 `chunk.image_id` | 图片 ID | 判断是否有图片，用于显示图片 |
| `chunk.content_with_weight` | 完整文本内容 | 显示在 Popover 中 |
| `chunk.highlight` | 高亮后的文本 | 显示在搜索结果列表中 |
| `chunk.positions` | 位置信息 | 用于在 PDF 中定位（如果支持） |
| `chunk.doc_type_kwd` | 文档类型 | 判断是图片/表格/文本，影响展示方式 |

**关键代码位置：**
- **搜索结果页**：`web/src/pages/next-search/search-view.tsx:195-242`
- **Chunk 卡片**：`web/src/pages/dataflow-result/components/chunk-card/index.tsx`
- **图片组件**：`web/src/components/image/index.tsx`
- **Markdown 内容**：`web/src/components/markdown-content/index.tsx`（聊天中的引用）

---

## 十二、问答/搜索整体流程（从用户提问到前端展示）

> 本节把 **"RAG 问答"** 从用户输入到前端展示的路径串起来，对应上面 Chunk 相关的检索/展示代码。

### 阶段 7.1：用户在前端发起搜索 / 提问

**位置：** `web/src/pages/next-search/search-view.tsx`

**流程：**
- 用户输入 `searchStr`，前端会调用后端搜索/检索接口（例如 `chunk_app.list_chunk` 或 SDK 文档检索接口）。

---

### 阶段 7.2：后端检索 Chunk（向量检索 + 关键词检索）

两类典型入口：

1. **文档内 Chunk 列表 / 过滤**  
   - **文件**：`api/apps/chunk_app.py`  
   - **函数**：`list_chunk()`  
   - **特点**：按 `doc_id` + `keywords` 搜索，常用于"查看某文档下的 Chunk"。

2. **知识库级别检索（对话/搜索）**  
   - **文件**：`api/apps/sdk/doc.py`  
   - **函数**：`list_chunks()` / `search_documents()`  
   - **特点**：按 `dataset_id` + `question` 在整个知识库里检索。

底层检索都依赖：

- **文件**：`rag/nlp/search.py`  
- **函数**：`retrieval()`  

其核心逻辑前文已展示：  
1. 用 Embedding 模型将 `question` 转成向量  
2. 在 DocStore（ES / Infinity）里做向量相似度 + BM25 混合检索  
3. 返回带 `content_with_weight`、`img_id`、`positions` 等字段的 chunks。

---

### 阶段 7.3：对话场景中的引用（References）

**位置：** `api/db/services/dialog_service.py` 和前端 Markdown 组件

**流程：**

- **后端**：`dialog_service` 把检索到的 chunks 放进 `reference` 字段，传给前端  
- **前端**：`web/src/components/markdown-content/index.tsx` / `web/src/components/floating-chat-widget-markdown.tsx`  
  - 解析引用索引 → 找到对应的 `chunkItem` 和 `imageId`
  - 如果 `showImage(doc_type)` 为真（例如 `image`/`table`），用 `<Image id={imageId} />` 展示图片  
  - 否则展示一个 `Popover`，里面是 chunk 的文本内容。

---

### 阶段 7.4：前端统一展示逻辑（搜索页 + 对话）

**搜索页：**
- `search-view.tsx` 直接用 `chunks` 数组渲染：
  - `chunk.img_id` → `ImageWithPopover` 显示图片
  - `chunk.highlight` → 列表中的短预览
  - `chunk.content_with_weight` → 弹窗中的完整文本

**对话页：**
- `MarkdownContent` / `FloatingChatWidgetMarkdown`：
  - 把回答中的引用标记替换为：
    - 引用图片：`<Image id={imageId} />`
    - 引用文本 Popover：展示 chunk 的 `content`/`content_with_weight`

这样，**无论是直接搜索 Chunk，还是在对话里看"引用来源"**，底层用的都是同一批：
- 向量库里的 Chunk（带 `img_id` + `positions`）  
- 图片服务 `/document/image/{img_id}`  
- 相似度/高亮等元数据。

---

## 十三、完整数据流向总结

```
1. MinerU 解析 PDF
   ↓
2. _transfer_to_sections() → Section (文本, 位置标签)
   ↓
3. parser.py: _pdf() → Bbox {text, image (PIL.Image), positions}
   ↓
4. parser.py: _invoke() → image2id() → JSON {text, img_id, positions}
   ↓
5. splitter.py: _invoke() → Section + Image (PIL.Image)
   ↓
6. naive_merge_with_images() → Chunk {text, image (PIL.Image), positions}
   ↓
7. image2id() → Chunk {text, img_id, positions}
   ↓
8. embedding() → Chunk {text, img_id, positions, q_768_vec}
   ↓
9. insert_es() → Elasticsearch/Infinity 向量库
   ↓
10. retrieval() → 检索匹配的 Chunks
   ↓
11. API: /api/chunk/list → 返回 JSON {chunks: [...]}
   ↓
12. 前端: search-view.tsx → 渲染 Chunk 列表
   ↓
13. Image 组件 → /api/document/image/{img_id} → 显示图片
```

---

## 十四、代码位置总结

| 阶段 | 文件 | 方法/函数 | 行数 |
|------|------|----------|------|
| MinerU 输出 → Section | `deepdoc/parser/mineru_parser.py` | `_transfer_to_sections()` | 797-855 |
| Section → Bbox | `rag/flow/parser/parser.py` | `_pdf()` (MinerU 分支) | 294-302 |
| 分类标记 | `rag/flow/parser/parser.py` | `_pdf()` | 361-371 |
| Bbox → JSON | `rag/flow/parser/parser.py` | `_invoke()` | 882-887 |
| JSON → Section | `rag/flow/splitter/splitter.py` | `_invoke()` | 111-115 |
| Section → Chunk | `rag/nlp/__init__.py` | `naive_merge_with_images()` | 922-1050 |
| Chunk → 向量库 | `rag/svr/task_executor.py` | `insert_es()` | 720-773 |
| Embedding 生成 | `rag/svr/task_executor.py` | `embedding()` | 936 |
| 向量库检索 | `rag/nlp/search.py` | `retrieval()` | 359-480 |
| API 返回 | `api/apps/chunk_app.py` | `list_chunk()` | 41-87 |
| 前端展示 | `web/src/pages/next-search/search-view.tsx` | `SearchingView` | 195-242 |
| 图片组件 | `web/src/components/image/index.tsx` | `Image` / `ImageWithPopover` | 11-35 |
| 对话引用 | `web/src/components/markdown-content/index.tsx` | `MarkdownContent` | 42-292 |

---

> **文档说明：**
>
> 这份文档完整覆盖了 RAGFlow 在 MinerU 场景下的整体流程：
>
> - ✅ **从文件上传 → 任务入队 → Task Executor → Pipeline（Parser+Splitter）**
> - ✅ **到 MinerU 解析 → Section/Bbox/Chunk → Embedding → 向量库**
> - ✅ **再到检索 → API 返回 → 前端展示（搜索页 & 对话页引用）**
>
> 可以视为 MinerU 场景下，RAGFlow 从"入库到检索展示"的完整流程说明。
