# MinerUParser 代码详解（`deepdoc/parser/mineru_parser.py`）

按调用顺序讲解 MinerU 解析流程：初始化与检查、运行 MinerU（API/可执行）、输出读取、内容转换、图像裁剪、顶层 `parse_pdf`，以及与 RagFlow `parser.py` 的衔接。

---

## 1. 顶层类与内容类型枚举

```python
class MinerUContentType(StrEnum):
    IMAGE = "image"
    TABLE = "table"
    TEXT = "text"
    EQUATION = "equation"
    CODE = "code"
    LIST = "list"
    DISCARDED = "discarded"

class MinerUParser(RAGFlowPdfParser):
    def __init__(self, mineru_path="mineru", mineru_api="http://host.docker.internal:9987", mineru_server_url=""):
        self.mineru_path = Path(mineru_path)
        self.mineru_api = mineru_api.rstrip("/")
        self.mineru_server_url = mineru_server_url.rstrip("/")
        self.using_api = False
        self.outlines = []
        self.logger = logging.getLogger(self.__class__.__name__)
```

- `MinerUContentType`：MinerU 输出的内容类型枚举。
- `MinerUParser`：持有 MinerU 可执行路径/API 地址；`using_api` 决定走 API 还是本地可执行。

---

## 2. 安装/服务可用性检查 `check_installation`
- 校验 backend 合法性：`pipeline / vlm-http-client / vlm-transformers / vlm-vllm-engine`。
- 如是 `vlm-http-client` 且传入 `server_url`，探测 `server_url + "/openapi.json"`；可用则 `using_api=False`（http client 模式）。
- 如配置了 `mineru_api`，也会探测 `/openapi.json`；可用即返回 True。
- 否则默认走可执行文件。返回 `(ok: bool, reason: str)`。

---

## 3. 运行 MinerU 总入口 `_run_mineru`
```python
def _run_mineru(...):
    if self.using_api:
        self._run_mineru_api(...)
    else:
        self._run_mineru_executable(...)
```

### 3.1 API 模式 `_run_mineru_api`
- POST 到 `{mineru_api}/file_parse`，上传 PDF。
- data 中开启：`return_md/return_middle_json/return_model_output/return_content_list/return_images/response_format_zip`。
- 返回 zip：保存 `output.zip` -> `_extract_zip_no_root` 解压到 `<output_dir>/<pdf_stem>/<method>/`。

### 3.2 可执行模式 `_run_mineru_executable`
- 命令：`mineru -p <pdf> -o <out_dir> -m <method> [-b backend] [-l lang] [-u server_url]`。
- 实时读取 stdout/stderr 日志；非零退出码抛错。

---

## 4. 页图准备与裁剪
### 4.1 `__images__`
- 用 pdfplumber 渲染 PDF 各页为图像，存入 `self.page_images`；支持 `page_from/page_to`、`zoomin`。

### 4.2 `extract_positions`
- 解析标签 `@@page\tleft\tright\ttop\tbottom##`，返回 `[(pages[], left, right, top, bottom), ...]`。

### 4.3 `crop`
- 根据位置信息在 `self.page_images` 上裁剪，支持跨页拼接，返回 PIL.Image（可选位置信息）。
- 校验无效位置/越界页，警告并跳过。

---

## 5. 读取 MinerU 输出 `_read_output`
- 按 backend/method 顺序查找 `<file_stem>_content_list.json`（auto/vlm/method）。
- 读 JSON 后，把 `img_path/table_img_path/equation_img_path` 补全为绝对路径。
- 未找到文件则抛 `FileNotFoundError`。

---

## 6. 内容转换 `_transfer_to_sections`
```python
match output["type"]:
  TEXT   -> output["text"]
  TABLE  -> table_body + caption + footnote（空则 “FAILED TO PARSE TABLE”）
  IMAGE  -> image_caption + image_footnote
  EQUATION -> text
  CODE   -> code_body + code_caption
  LIST   -> list_items
  DISCARDED -> pass
```
- 返回列表：默认 `(section, line_tag)`；`parse_method="manual"` 返回 `(section, type, line_tag)`；`parse_method="paper"` 将 `line_tag` 拼到文本末尾。
- `_transfer_to_tables` 当前返回空列表。

---

## 7. 顶层解析 `parse_pdf`
流程：
1) 预处理文件名，binary 落盘为临时 PDF。  
2) 准备输出目录（未指定则临时目录；`delete_output` 控制清理）。  
3) `__images__` 预渲染页图。  
4) `_run_mineru(...)` 调用 MinerU（API/可执行）。  
5) `_read_output(...)` 读取 MinerU JSON。  
6) `_transfer_to_sections(...)` 转成 sections；tables 目前为空。  
7) finally 清理临时 PDF/输出目录（若配置删除）。  

返回 `(sections, tables)`；`sections` 形如 `(text, line_tag)` 或含 type，取决于 `parse_method`。

---

## 8. 与 RagFlow `parser.py` 的衔接
- RagFlow 在 `rag/flow/parser/parser.py` 的 MinerU 分支调用 `parse_pdf()`，获得 `(lines, tables)`。
- RagFlow 随后用 `crop()` + `extract_positions()` 生成 bboxes（文本、图片、坐标），再分类、上下文增强、输出。
- 若要保留 MinerU 类型信息，可调整 `parse_method` 或在 RagFlow 侧读取 `_transfer_to_sections` 的 `output["type"]`。

---

## 🔑 关键点小结
- **可用性检查**：`check_installation()` 决定走 API 还是可执行，并设置 `using_api`。  
- **运行路径**：`_run_mineru` 统一入口，分 API / 可执行。  
- **输出读取**：`_read_output` 查找 `_content_list.json`，补全图片路径。  
- **类型转换**：`_transfer_to_sections` 将 MinerU 输出按类型拼装文本，保留坐标标签。  
- **图像裁剪**：`__images__` + `crop` 支持跨页。  
- **清理策略**：`delete_output` 控制临时目录清理。  


