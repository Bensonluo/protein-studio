# 第三方组件与参考数据

- **3Dmol.js**：WebGL 分子展示库，BSD-3-Clause。实际随项目分发的文件位于 `web/vendor/3Dmol-min.js`；原始和构建依赖许可证分别保留为 `3Dmol-LICENSE.txt`、`3Dmol-min.js.LICENSE.txt`。来源 https://3dmol.org/ ，下载日期 2026-10-23。引用：Rego N, Koes D. *3Dmol.js: molecular visualization with WebGL*. Bioinformatics 31(8):1322–1324 (2015), DOI:10.1093/bioinformatics/btu829。
- **AlphaFold DB P0CG47**：公开历史预测结构及其置信度；保留 API 返回的数据库版本、原始 URL 和下载元数据。模型输出与平台代码的授权分别处理。来源 https://alphafold.ebi.ac.uk/entry/P0CG47 。
- **RCSB PDB 1UBQ、1CRN**：公开实验结构示例，原始 PDB 文件保留来源信息。来源 https://www.rcsb.org/structure/1UBQ 、https://www.rcsb.org/structure/1CRN 。
- **AF2 / AF3 / ColabFold 等模型**：本项目通过用户配置的运行环境调用，没有分发模型权重或遗传数据库。各项目代码和权重许可的原始链接见 `sources.json` 与 `docs/research.md`。
- **ijson 3.6.0**：置信度 JSON 的流式解析依赖，BSD-3-Clause AND ISC；通过 `requirements.txt` 安装，不随源码包分发二进制。来源：[官方包与迭代解析文档](https://pypi.org/project/ijson/)。

第三方文件的 SHA-256 在发布前生成到 `artifacts/verification.json`。本项目声明的 MIT 许可证适用于新编写的工作台代码，不修改第三方许可。
