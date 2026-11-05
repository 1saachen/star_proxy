import { readFile, writeFile, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { Marked } from 'marked';
import { modelGuide, section } from './model-guides.mjs';
import { curlSamples } from './code-samples.mjs';
import { supplierModels, supplierPageId, supplierGroups } from './supplier-guides.mjs';
import { vegaMaterialOperations, vegaMaterialReference, vegaMaterialIntro, vegaMaterialSection } from './vega-material-guides.mjs';
export { vegaMaterialOperations } from './vega-material-guides.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
export const root = path.resolve(here, '../..');
export const modelCatalog = JSON.parse(await readFile(path.join(here, 'models.json'), 'utf8'));
export const supplierCatalog = modelCatalog.flatMap(supplierModels);
export const pages = [
  ['start', '开始接入', '快速开始', 'backend/CLIENT_API.md'],
  ['basics', '开始接入', '鉴权与通用约定', 'backend/docs/client-api/basics.md'],
  ['models', '开始接入', '模型目录与价格', 'backend/docs/client-api/models.md'],
  ['text', '模型接口', '文本与视觉', 'backend/docs/client-api/text.md'],
  ['images', '模型接口', '图片生成', 'backend/docs/client-api/images.md'],
  ['videos', '模型接口', '异步视频', 'backend/docs/client-api/videos.md'],
  ['embeddings', '模型接口', '向量', 'backend/docs/client-api/embeddings.md'],
  ['audio', '模型接口', '语音与音频', 'backend/docs/client-api/audio.md'],
  ['vega', '模型接口', 'Vega 视频与素材', 'backend/docs/client-api/vega.md'],
  ['vega-materials', '素材与帮助', 'Vega 素材流程与规格', 'backend/docs/client-api/vega-materials.md'],
  ['vega-image-vl', '模型接口', 'Vega 生图与理解', 'backend/docs/client-api/vega-image-vl.md'],
  ['assets', '素材与帮助', '素材上传与素材库', 'backend/docs/client-api/assets.md'],
  ['errors', '素材与帮助', '错误与安全重试', 'backend/docs/client-api/errors.md'],
];
export const assetOperations = [
  ['asset-upload', '上传本地素材', '上传文件', 'POST', '/api/asset/upload-file'],
  ['group-create', '创建素材组', '创建素材组', 'POST', '/api/asset-group/create'],
  ['group-list', '查询素材组列表', '查询素材组列表', 'GET', '/api/asset-group/list'],
  ['group-get', '查询单个素材组', '查询素材组详情', 'GET', '/api/asset-group/get'],
  ['group-update', '修改素材组', '修改素材组', 'PUT', '/api/asset-group/update'],
  ['group-delete', '删除素材组', '删除素材组', 'DELETE', '/api/asset-group/delete'],
  ['asset-create', '登记素材', '登记素材', 'POST', '/api/asset/create'],
  ['asset-list', '查询素材列表', '查询素材列表', 'GET', '/api/asset/list'],
  ['asset-get', '查询单个素材', '查询素材状态', 'GET', '/api/asset/get'],
  ['asset-update', '修改素材名称', '修改素材名称', 'PUT', '/api/asset/update'],
  ['asset-delete', '删除素材', '删除素材', 'DELETE', '/api/asset/delete'],
];

export async function loadSources() {
  const sources = await Promise.all(pages.map(async ([id, group, label, file]) => ({
    id, group, label, file, provider: id === 'assets' ? 'volcengine_ark' : id === 'vega-materials' ? 'vega' : undefined,
    source: (id === 'vega-materials' ? vegaMaterialReference : await readFile(path.join(root, file), 'utf8')).replace(/\r\n/g, '\n'), kind: 'reference',
  })));
  const refs = Object.fromEntries(sources.map(page => [page.id, page.source]));
  for (const model of supplierCatalog) {
    sources.push({ id: supplierPageId(model), group: model.supplierLabel, provider: model.provider,
      label: model.id, navLabel: model.id,
      file: 'backend/docs/client-api/model-reference.md', kind: 'model',
      source: modelGuide(model, refs).replace(/^## /gm, '# ').replace(/^### /gm, '## '),
    });
  }
  for (const [id, heading, label, method, endpoint] of assetOperations) {
    sources.push({ id, group: '火山引擎', provider: 'volcengine_ark', label: label + ' · 火山引擎', navLabel: label, method, endpoint, kind: 'asset',
      file: 'backend/docs/client-api/assets.md', source: '# ' + label + '\n\n' +
        '供应商：火山引擎。使用星辰平台业务 Key，素材范围由当前项目确定。\n\n' +
        '[素材流程与规格](assets.md#素材接口概览) · [鉴权](basics.md) · [错误与重试](errors.md)\n\n' +
        section(refs.assets, '## ' + heading).replace(/^### /gm, '## '),
    });
  }
  for (const operation of vegaMaterialOperations) {
    sources.push({ id: operation.id, group: 'Vega', provider: 'vega', label: operation.label,
      navLabel: operation.label, method: 'POST', endpoint: '/v1/vega/' + operation.action, kind: 'asset',
      file: 'backend/docs/client-api/vega-materials.md', source: '# ' + operation.label + '\n\n' +
        vegaMaterialIntro + '\n\n[Vega素材流程](vega-materials.md#素材流程与接口概览) · [鉴权](basics.md) · [错误与重试](errors.md)\n\n' +
        vegaMaterialSection(operation).replace(/^### /gm, '## '),
    });
  }
  return sources;
}
export const escapeHtml = (s) => String(s).replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));
export const slug = (text) => text.replace(/<[^>]*>/g, '').replace(/`/g, '').toLowerCase()
  .replace(/[^\p{L}\p{N}\p{M}\s_-]/gu, '').replace(/ /g, '-');

export async function build() {
  const sources = await loadSources();
  const documentVersion = sources.find(page => page.id === 'start').source.match(/^版本：([^\s·]+)/m)?.[1];
  if (!documentVersion) throw new Error('Client API document version is missing');
  const css = (await readFile(path.join(here, 'style.css'), 'utf8')).replace(/\r\n/g, '\n');
  const js = (await readFile(path.join(here, 'reader.js'), 'utf8')).replace(/\r\n/g, '\n');
  const logo = await readFile(path.join(here, 'ruichi-logo.png'));
  const logoUrl = `data:image/png;base64,${logo.toString('base64')}`;
  const headings = new Map();
  for (const page of sources) {
    const counts = new Map();
    page.headings = [];
    const parser = new Marked({ gfm: true });
    for (const token of parser.lexer(page.source)) {
      if (token.type !== 'heading') continue;
      const base = slug(token.text);
      const n = counts.get(base) ?? 0;
      counts.set(base, n + 1);
      const anchor = base + (n ? `-${n}` : '');
      page.headings.push({ anchor, id: `${page.id}--${anchor}`, depth: token.depth, text: token.text });
    }
    const entry = headings.get(page.file) || { id: page.id, entries: new Map() };
    for (const heading of page.headings) entry.entries.set(heading.anchor, heading);
    headings.set(page.file, entry);
  }
  function resolveLink(page, href) {
    if (/^https:\/\//.test(href)) return href;
    const [file, fragment] = href.split('#');
    // The HTML entry links back to this self-contained document, not a Markdown file.
    if (file === 'CLIENT_API.html') return '#start';
    const dest = file ? path.posix.normalize(path.posix.join(path.posix.dirname(page.file), file)) : page.file;
    const target = headings.get(dest);
    if (!target) throw new Error(`Unbundled or unsafe link: ${page.file} -> ${href}`);
    if (!fragment) return `#${target.id}`;
    const heading = target.entries.get(decodeURIComponent(fragment));
    if (!heading) throw new Error(`Broken anchor: ${page.file} -> ${href}`);
    return `#${heading.id}`;
  }
  const articles = sources.map(page => {
    let index = 0;
    const parser = new Marked({ gfm: true, renderer: {
      heading({ tokens, depth }) {
        const heading = page.headings[index++];
        return `<h${depth} id="${escapeHtml(heading.id)}">${this.parser.parseInline(tokens)}</h${depth}>\n`;
      },
      code({ text, lang }) {
        const samples = lang === 'bash' ? curlSamples(text) : [];
        if (samples.length > 1) return `<div class="code-block"><div class="code-head"><div class="code-tabs" role="tablist" aria-label="请求示例语言">${samples.map((s, i) => `<button type="button" class="code-tab" role="tab" aria-selected="${!i}" tabindex="${i ? -1 : 0}" data-language="${s.language}">${s.label}</button>`).join('')}</div><button type="button" class="copy-code" aria-label="复制代码">复制代码</button></div>${samples.map((s, i) => `<pre tabindex="0" data-language="${s.language}" ${i ? 'hidden' : ''}><code>${escapeHtml(s.text)}</code></pre>`).join('')}</div>\n`;
        return `<div class="code-block"><div class="code-head"><span>${escapeHtml(lang || 'text')}</span><button type="button" class="copy-code" aria-label="复制代码">复制代码</button></div><pre tabindex="0"><code>${escapeHtml(text)}</code></pre></div>\n`;
      },
      link({ href, tokens }) {
        const url = resolveLink(page, href);
        return `<a href="${escapeHtml(url)}">${this.parser.parseInline(tokens)}</a>`;
      },
      html({ text }) {
        if (/^<br\s*\/?\s*>$/i.test(text.trim())) return '<br>';
        throw new Error(`Raw HTML is not allowed in ${page.file}: ${text.slice(0, 40)}`);
      },
    }});
    const body = parser.parse(page.source).replace(/<table>/g, '<div class="table-wrap" tabindex="0"><table>')
      .replace(/<\/table>/g, '</table></div>');
    const endpoint = page.endpoint || page.source.match(/`(GET|POST|PUT|DELETE) (\/[^`\n ]+)`/)?.[2];
    const method = page.method || page.source.match(/`(GET|POST|PUT|DELETE) (\/[^`\n ]+)`/)?.[1];
    let endpointBar = '';
    if (endpoint && ['model', 'asset'].includes(page.kind)) {
      endpointBar = `<div class="endpoint-bar"><span class="method method-${method.toLowerCase()}">${method}</span><code>${escapeHtml(endpoint)}</code><button type="button" class="copy-path" data-path="${escapeHtml(endpoint)}">复制路径</button></div>`;
      endpointBar += `<div class="api-note">生产环境 <code>https://api.richbest.cn</code> · Bearer API Key</div>`;
      endpointBar += `<section class="request-headers"><h2 id="${page.id}--请求头">请求头</h2><div class="field-row"><div><code>Authorization</code><span class="field-type">string</span><b>必需</b></div><p>Bearer YOUR_API_KEY · 使用管理员签发的星辰平台业务 Key</p></div>${method !== 'GET' && method !== 'DELETE' ? `<div class="field-row"><div><code>Content-Type</code><span class="field-type">string</span><b>必需</b></div><p>${page.id === 'asset-upload' ? 'multipart/form-data（boundary 由客户端生成）' : 'application/json'}</p></div>` : ''}</section>`;
    }
    const enhanced = endpointBar ? body.replace(/(<\/h1>)/, '$1' + endpointBar) : body;
    return `<article class="docs-page" id="${page.id}" data-kind="${page.kind}" data-label="${escapeHtml(page.label)}"${page.provider ? ` data-provider="${page.provider}"` : ''}>${enhanced}</article>`;
  }).join('\n');
  const navLink = page => `<a class="page-link" href="#${page.id}" data-page="${page.id}" title="${escapeHtml(page.label)}"><span>${escapeHtml(page.navLabel || page.label)}</span>${['asset', 'model'].includes(page.kind) ? `<small class="method method-${(page.method || 'POST').toLowerCase()}">${page.method || 'POST'}</small>` : ''}</a>`;
  const intro = sources.filter(p => ['start', 'basics', 'models', 'errors'].includes(p.id)).map(navLink).join('\n');
  const assetNav = (provider, referenceId) => {
    const operations = sources.filter(p => p.kind === 'asset' && p.provider === provider);
    return `<details class="nav-folder asset-folder"><summary>素材接口<span>${operations.length}</span></summary><div>${navLink(sources.find(p => p.id === referenceId))}${operations.map(navLink).join('\n')}</div></details>`;
  };
  const modelNav = supplierGroups.map(group => {
    const models = group.providers.flatMap(provider => sources.filter(p => p.kind === 'model' && p.provider === provider));
    const materialCount = sources.filter(p => p.kind === 'asset' && group.providers.includes(p.provider)).length;
    const count = models.length + ' 模型' + (materialCount ? ' / ' + materialCount + ' 素材' : '');
    const entries = models.map(navLink).join('\n');
    const materialNav = group.id === 'volcengine' ? assetNav('volcengine_ark', 'assets') : group.id === 'vega' ? assetNav('vega', 'vega-materials') : '';
    const channels = materialNav ? `<p class="nav-label">模型接口</p>${entries}${materialNav}` : entries;
    return `<details class="nav-folder supplier-folder" data-supplier="${group.id}"><summary>${escapeHtml(group.label)}<span>${count}</span></summary><div>${channels}</div></details>`;
  }).join('\n');
  const referenceNav = `<details class="nav-folder"><summary>通用协议参考</summary><div>${sources.filter(p => ['text', 'images', 'videos', 'audio', 'embeddings'].includes(p.id)).map(navLink).join('\n')}</div></details>`;
  const nav = intro + '<p class="nav-label">供应商 API</p>' + modelNav + '<p class="nav-label">其他参考</p>' + referenceNav;
  const sourceHash = createHash('sha256').update(sources.map(p => p.file + '\n' + p.source).join('\n') + css + js).update(logo).digest('hex');
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'">
<meta name="description" content="星辰平台 API：模型目录、价格、文本、图片、视频、向量、音频与素材接口参考">
<meta name="source-hash" content="${sourceHash}"><title>星辰平台 · API 接口文档</title><style>${css}</style></head>
<body><a class="skip-link" href="#main">跳到文档正文</a>
<header class="topbar"><a class="brand" href="#start"><span class="brand-mark"><img src="${logoUrl}" alt="瑞池传媒" width="32" height="48"></span><span>星辰平台 <small>开发者文档</small></span></a>
<div class="top-actions"><span class="version">文档 ${escapeHtml(documentVersion)}</span><button id="print" type="button">打印 / PDF</button><button id="menu" type="button" aria-controls="sidebar" aria-expanded="false">目录</button></div></header>
<div class="layout"><aside id="sidebar" aria-label="文档目录"><label class="search-label" for="search">搜索文档</label><div class="search-box"><input id="search" type="search" placeholder="模型、接口、参数或错误码" autocomplete="off"><kbd>/</kbd></div>
<div id="search-status" role="status" aria-live="polite"></div><nav id="search-results" aria-label="搜索结果" hidden></nav><nav id="navigation" aria-label="模型与素材接口">${nav}</nav>
<div class="sidebar-foot"><span class="dot"></span>HTTP API · Bearer Key<br><code>api.richbest.cn</code></div></aside>
<main id="main" tabindex="-1"><div class="breadcrumb">开发者文档 <span>/</span> <span id="current-page">快速开始</span></div><noscript><p>当前已展示全部文档；启用 JavaScript 后可使用分类导航、搜索和代码复制。</p></noscript>${articles}<footer>星辰平台 API · 业务 Key 仅保存在你的服务器中</footer></main>
<aside class="toc"><span>本页内容</span><nav id="toc" aria-label="本页目录"></nav></aside></div>
<div id="toast" role="status" aria-live="polite"></div><script>${js}</script></body></html>\n`;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const sources = await loadSources();
  const modelMarkdown = '# 按供应商和模型接入\n\n[文档首页](../../CLIENT_API.md) · [模型目录与价格](models.md) · [素材接口](assets.md)\n\n' + supplierGroups.map(group => '## ' + group.label + '\n\n' + sources.filter(p => p.kind === 'model' && group.providers.includes(p.provider)).map(p => p.source.replace(/^## /gm, '#### ').replace(/^# /gm, '### ')).join('\n')).join('\n');
  const modelTarget = path.join(root, 'backend/docs/client-api/model-reference.md');
  const materialTarget = path.join(root, 'backend/docs/client-api/vega-materials.md');
  if (process.argv.includes('--check')) {
    if (await readFile(materialTarget, 'utf8') !== vegaMaterialReference) throw new Error('Vega material reference is stale; run npm run docs:build');
  } else await writeFile(materialTarget, vegaMaterialReference, 'utf8');
  if (process.argv.includes('--check')) {
    if (await readFile(modelTarget, 'utf8') !== modelMarkdown) throw new Error('Model reference is stale; run npm run docs:build');
  } else await writeFile(modelTarget, modelMarkdown, 'utf8');
  const html = await build();
  const version = createHash('sha256').update(html).digest('hex').slice(0, 16);
  const manifest = '// Generated by npm run docs:build. Do not edit manually.\nexport const CLIENT_DOCS_URL = "/docs/client-api.html?v=' + version + '";\n';
  const manifestPath = path.join(root, 'app/client-docs-manifest.ts');
  if (process.argv.includes('--check')) {
    if (await readFile(manifestPath, 'utf8') !== manifest) throw new Error('Console document version is stale; run npm run docs:build');
  } else await writeFile(manifestPath, manifest, 'utf8');
  for (const file of ['backend/CLIENT_API.html', 'public/docs/client-api.html']) {
    const target = path.join(root, file);
    if (process.argv.includes('--check')) {
      if (await readFile(target, 'utf8') !== html) throw new Error(`${file} is stale; run npm run docs:build`);
    } else {
      await mkdir(path.dirname(target), { recursive: true });
      await writeFile(target, html, 'utf8');
    }
  }
  console.log(process.argv.includes('--check') ? 'Client HTML copies are current and identical.' : 'Built standalone and console client HTML.');
}
