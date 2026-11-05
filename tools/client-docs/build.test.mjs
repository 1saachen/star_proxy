import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import vm from 'node:vm';
import { createHash } from 'node:crypto';
import { JSDOM, VirtualConsole } from 'jsdom';
import { build, pages, root, escapeHtml, modelCatalog, assetOperations, vegaMaterialOperations, supplierCatalog } from './build.mjs';
import { supplierPageId, supplierGroups } from './supplier-guides.mjs';
import { modelGuide } from './model-guides.mjs';

test('generated document bytes use platform-independent line endings', async () => {
  const html = await build();
  assert.equal(html.includes('\r'), false);
});

test('domestic Seedance Draft has distinct idempotency keys and no repeated inherited fields', () => {
  const model = modelCatalog.find(item => item.id === 'doubao-seedance-2.5');
  assert.equal(model.capabilities.draft, true);
  const guide = modelGuide(model, {});
  assert.match(guide, /Idempotency-Key: seedance-draft-001/);
  assert.match(guide, /Idempotency-Key: seedance-final-001/);
  const requests = [...guide.matchAll(/```bash\n([\s\S]*?)```/g)].flatMap(match => {
    const raw = match[1].match(/-d '([\s\S]+)'/)?.[1] ?? match[1].match(/-d "([\s\S]+)"/)?.[1]?.replaceAll('\\"', '"');
    return raw ? [JSON.parse(raw)] : [];
  });
  const draft = requests.find(body => body.draft === true);
  const final = requests.find(body => body.content?.[0]?.type === 'draft_task');
  assert.equal(draft.resolution, '480p');
  assert.equal(final.resolution, '1080p');
  assert.deepEqual(Object.keys(final).sort(), ['content', 'model', 'resolution']);
  for (const item of modelCatalog.filter(item => item.id.startsWith('dreamina-seedance'))) {
    assert.notEqual(item.capabilities.draft, true);
  }
});

const html = await build();
function reader(t, options = {}) {
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on('jsdomError', error => errors.push(error.message));
  const dom = new JSDOM(html, {
    url: options.url || 'file:///docs/CLIENT_API.html', runScripts: 'dangerously', virtualConsole,
    beforeParse(window) {
      window.scrollTo = () => {};
      window.HTMLElement.prototype.scrollIntoView = () => {};
      options.beforeParse?.(window);
    },
  });
  t.after(() => { dom.window.close(); assert.deepEqual(errors, []); });
  return dom.window;
}

test('HTML is deterministic, self-contained, and both delivery copies are current', async () => {
  assert.equal(html, await build());
  assert.equal(html, await readFile(path.join(root, 'backend/CLIENT_API.html'), 'utf8'));
  assert.equal(html, await readFile(path.join(root, 'public/docs/client-api.html'), 'utf8'));
  const version = createHash('sha256').update(html).digest('hex').slice(0, 16);
  assert.match(await readFile(path.join(root, 'app/client-docs-manifest.ts'), 'utf8'), new RegExp('client-api\\.html\\?v=' + version));
  const dom = new JSDOM(html);
  const documentVersion = (await readFile(path.join(root, 'backend/CLIENT_API.md'), 'utf8')).match(/^版本：([^\s·]+)/m)[1];
  assert.equal(dom.window.document.querySelector('.version').textContent, '文档 ' + documentVersion);
  assert.equal(dom.window.document.querySelectorAll('.docs-page').length, pages.length + supplierCatalog.length + assetOperations.length + vegaMaterialOperations.length);
  assert.equal(dom.window.document.querySelectorAll('script[src], link[rel="stylesheet"], iframe, form').length, 0);
  assert.match(dom.window.document.querySelector('meta[http-equiv]').content, /connect-src 'none'/);
  const ids = [...dom.window.document.querySelectorAll('[id]')].map(el => el.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const link of dom.window.document.querySelectorAll('a[href^="#"]')) {
    assert.ok(dom.window.document.getElementById(decodeURIComponent(link.hash.slice(1))), link.href);
  }
  assert.match(escapeHtml('<script>"&'), /&lt;script&gt;&quot;&amp;/);
  dom.window.close();
});

test('brand uses the original embedded Ruichi logo without external image requests or cropping', async t => {
  const window = reader(t);
  const mark = window.document.querySelector('.brand-mark');
  const image = mark.querySelector('img');
  assert.ok(image);
  assert.equal(mark.textContent, '');
  assert.equal(image.alt, '瑞池传媒');
  assert.equal(image.width, 32);
  assert.equal(image.height, 48);
  assert.match(image.src, /^data:image\/png;base64,/);
  const original = await readFile(path.join(root, 'tools/client-docs/ruichi-logo.png'));
  assert.deepEqual(Buffer.from(image.src.split(',')[1], 'base64'), original);
  assert.equal(window.getComputedStyle(image).objectFit, 'contain');
  assert.equal(window.getComputedStyle(mark).backgroundColor, 'rgba(0, 0, 0, 0)');
  assert.equal(window.document.querySelectorAll('img:not([src^="data:"])').length, 0);
});

test('supplier then model navigation includes duplicate aliases without a channel isolation layer', t => {
  const window = reader(t);
  const { document } = window;
  assert.equal(supplierCatalog.length, 59);
  for (const variant of supplierCatalog) {
      const id = supplierPageId(variant);
      window.location.hash = '#' + id;
      window.dispatchEvent(new window.HashChangeEvent('hashchange'));
      const page = document.querySelector('.docs-page:not([hidden])');
      assert.equal(page.id, id);
      assert.equal(page.dataset.label, variant.id);
      assert.equal(page.querySelector('h1').textContent, variant.id);
      assert.doesNotMatch(page.textContent, /供应商：|ModelTop 供应商|BytePlus 供应商/);
      assert.equal(page.dataset.provider, variant.provider);
      assert.ok(page.querySelector('.endpoint-bar'));
      const link = document.querySelector('[aria-current="page"]');
      let folder = link.closest('details');
      let count = 0;
      while (folder) { assert.equal(folder.open, true); count++; folder = folder.parentElement.closest('details'); }
      const groupedProvider = variant.provider.startsWith('volcengine_');
      assert.equal(count, 1);
      assert.equal(link.closest('.supplier-folder').dataset.supplier, groupedProvider ? 'volcengine' : variant.provider);
      if (groupedProvider) assert.equal(link.closest('.provider-folder'), null);
      if (variant.provider === 'modeltop') {
        assert.ok(![...page.querySelectorAll('pre code')].some(code => code.textContent.includes('curl -X DELETE')));
        assert.ok(!page.textContent.includes('BytePlus 渠道不接受'));
      }
  }
  assert.equal(document.querySelectorAll('.supplier-folder').length, supplierGroups.length);
  assert.equal(document.querySelectorAll('.supplier-folder').length, 7);
  assert.equal(document.querySelector('.speech-folder'), null);
  assert.equal(document.querySelectorAll('.supplier-folder[data-supplier="volcengine"] > div > .page-link').length, 27);
  const volcanoModelLinks = [...document.querySelectorAll('.supplier-folder[data-supplier="volcengine"] > div > .page-link')];
  assert.equal(volcanoModelLinks.filter(link => document.getElementById(link.dataset.page).dataset.provider === 'volcengine_speech').length, 3);
  assert.equal(document.querySelectorAll('.channel-folder').length, 0);
});

test('original material operations remain under Volcengine and deep links expand the supplier folder', t => {
  const window = reader(t);
  const { document } = window;
  const assets = document.querySelector('[data-supplier="volcengine"] .asset-folder');
  assert.equal(assets.closest('.supplier-folder').dataset.supplier, 'volcengine');
  assert.equal(assets.closest('.provider-folder'), null);
  assert.equal(document.querySelectorAll('.asset-folder').length, 2);
  for (const [id] of assetOperations) {
    const link = document.querySelector('.page-link[data-page="' + id + '"]');
    assert.equal(link.closest('.supplier-folder').dataset.supplier, 'volcengine');
    assert.equal(link.closest('.provider-folder'), null);
    const article = document.getElementById(id);
    assert.equal(article.dataset.provider, 'volcengine_ark');
    assert.match(article.textContent, /供应商：火山引擎/);
  }
  window.location.hash = '#asset-get';
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  assert.equal(assets.open, true);
  assert.equal(assets.closest('.supplier-folder').open, true);
  assert.equal(document.querySelector('.docs-page:not([hidden])').id, 'asset-get');
});

test('all ten Vega material and liveness operations have supplier-local pages, parameters and examples', t => {
  const window = reader(t);
  const { document } = window;
  assert.equal(vegaMaterialOperations.length, 10);
  const folder = document.querySelector('[data-supplier="vega"] .asset-folder');
  assert.ok(folder);
  assert.match(folder.querySelector('summary').textContent, /素材接口10/);
  for (const operation of vegaMaterialOperations) {
    const link = folder.querySelector('[data-page="' + operation.id + '"]');
    assert.ok(link, operation.action);
    window.location.hash = '#' + operation.id;
    window.dispatchEvent(new window.HashChangeEvent('hashchange'));
    const article = document.querySelector('.docs-page:not([hidden])');
    assert.equal(article.id, operation.id);
    assert.equal(article.dataset.provider, 'vega');
    assert.equal(article.querySelector('.endpoint-bar .method').textContent, 'POST');
    assert.equal(article.querySelector('.endpoint-bar code').textContent, '/v1/vega/' + operation.action);
    assert.match(article.textContent, /请求参数/);
    assert.match(article.textContent, /响应与后续处理/);
    assert.match(article.textContent, /model/);
    assert.ok(article.querySelector('pre[data-language="shell"]'));
    assert.ok(article.querySelector('pre[data-language="javascript"]'));
    if (operation.create) assert.match(article.textContent, /Idempotency-Key/);
    assert.equal(folder.open, true);
    assert.equal(folder.closest('.supplier-folder').open, true);
  }
  const volcanicFolder = document.querySelector('[data-supplier="volcengine"] .asset-folder');
  assert.equal(volcanicFolder.querySelectorAll('[data-page^="vega-"]').length, 0);
});

test('JavaScript examples execute offline with matching URLs, headers, bodies and response readers', async () => {
  const dom = new JSDOM(html);
  try {
    const blocks = [...dom.window.document.querySelectorAll('.code-block')].filter(block => block.querySelector('pre[data-language="javascript"]'));
    assert.ok(blocks.length > 150);
    for (const block of blocks) {
      const shell = block.querySelector('pre[data-language="shell"] code').textContent;
      const script = block.querySelector('pre[data-language="javascript"] code').textContent;
      const body = shell.match(/-d '([\s\S]+)'/)?.[1] ?? shell.match(/-d "([\s\S]+)"/)?.[1]?.replaceAll('\\"', '"');
      const endpoint = shell.match(/"\$BASE_URL([^"]+)"/)[1].replace('$TASK_ID', 'mock-task').replace('$DRAFT_TASK_ID', 'vid_mock_draft');
      const method = shell.match(/-X (\w+)/)?.[1] || (body ? 'POST' : 'GET');
      const output = shell.match(/--output ([\w.-]+)/)?.[1];
      const expected = body ? JSON.parse(body.replaceAll('$DRAFT_TASK_ID', 'vid_mock_draft')) : undefined;
      const calls = [];
      const writes = [];
      const source = script.replace('const { writeFile } = await import("node:fs/promises");', 'const writeFile = mockWriteFile;');
      const context = vm.createContext({
        Buffer, process: { env: { API_KEY: 'mock-key', TASK_ID: 'mock-task', DRAFT_TASK_ID:'vid_mock_draft' }, stdout: { write() {} } }, console: { log() {} },
        mockWriteFile: async (file, bytes) => { writes.push([file, bytes.toString()]); },
        fetch: async (url, options) => {
          calls.push({ url, options });
          return {
            ok: true, status: method === 'DELETE' ? 204 : 200,
            json: async () => { assert.notEqual(method, 'DELETE'); assert.ok(!output); assert.ok(!expected?.stream); return {}; },
            arrayBuffer: async () => Buffer.from('mock-audio'),
            body: (async function* () { assert.equal(expected?.stream, true); yield Buffer.from('data: [DONE]\n\n'); })(),
          };
        },
      });
      await new vm.Script('(async () => {\n' + source + '\n})()').runInContext(context);
      assert.equal(calls.length, 1);
      assert.equal(calls[0].url, 'https://api.richbest.cn' + endpoint);
      assert.equal(calls[0].options.method, method);
      assert.equal(calls[0].options.headers.Authorization, 'Bearer mock-key');
      assert.deepEqual(calls[0].options.body ? JSON.parse(calls[0].options.body) : undefined, expected);
      assert.deepEqual(writes, output ? [[output, 'mock-audio']] : []);
    }
  } finally { dom.window.close(); }
});

test('every model and original asset operation has a dedicated navigable reference', t => {
  const window = reader(t);
  const { document } = window;
  for (const model of modelCatalog) {
    const id = 'model-' + model.id;
    const article = document.getElementById(id);
    assert.ok(article, model.id);
    assert.ok(article.querySelector('.endpoint-bar'), model.id);
    assert.match(article.textContent, /请求|参数/);
    assert.match(article.textContent, /响应|结果/);
    assert.ok([...document.querySelectorAll('.page-link')].some(link => link.dataset.page === id));
  }
  for (const [id, , , method, endpoint] of assetOperations) {
    const article = document.getElementById(id);
    assert.ok(article, id);
    assert.equal(article.querySelector('.endpoint-bar .method').textContent, method);
    assert.equal(article.querySelector('.endpoint-bar code').textContent, endpoint);
  }
  window.location.hash = '#model-gpt-image-2.5-flare';
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  const active = document.querySelector('[aria-current="page"]');
  assert.equal(active.dataset.page, 'model-gpt-image-2.5-flare');
  assert.equal(active.closest('details').open, true);
  window.location.hash = '#asset-get';
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  assert.equal(document.querySelector('.docs-page:not([hidden])').id, 'asset-get');
});

test('navigation and deep links show only the requested page', t => {
  const window = reader(t, { url: 'file:///docs/CLIENT_API.html#images--gpt-image-与渠道差异' });
  const { document } = window;
  assert.equal(document.querySelector('.docs-page:not([hidden])').id, 'images');
  assert.equal(document.querySelector('[aria-current="page"]').dataset.page, 'images');
  assert.ok(document.querySelector('#toc a[href="#images--创建图片"]'));
  window.location.hash = '#videos';
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  assert.equal(document.querySelector('.docs-page:not([hidden])').id, 'videos');
  window.location.hash = '#not-a-real-page';
  window.dispatchEvent(new window.HashChangeEvent('hashchange'));
  assert.equal(document.querySelector('.docs-page:not([hidden])').id, 'start');
});

test('search covers body, model names, routes, and error codes without injecting markup', t => {
  const window = reader(t);
  const { document } = window;
  const search = document.getElementById('search');
  for (const query of ['GPT-IMAGE-2.5-FLARE', '/v1/audio/speech', 'idempotency_outcome_unknown', '30 秒']) {
    search.value = query;
    search.dispatchEvent(new window.Event('input'));
    assert.ok(document.querySelectorAll('#search-results a').length > 0, query);
    assert.equal(document.getElementById('navigation').hidden, true);
  }
  search.value = '<img src=x onerror=alert(1)>';
  search.dispatchEvent(new window.Event('input'));
  assert.equal(document.querySelectorAll('#search-results img').length, 0);
  assert.match(document.getElementById('search-status').textContent, /没有匹配/);
  search.value = '';
  search.dispatchEvent(new window.Event('input'));
  assert.equal(document.getElementById('navigation').hidden, false);
});

test('code copy preserves source exactly and reports success', async t => {
  let copied;
  const window = reader(t, { beforeParse(w) {
    Object.defineProperty(w.navigator, 'clipboard', { value: { writeText: async text => { copied = text; } } });
  }});
  const block = window.document.querySelector('.code-block');
  block.querySelector('.copy-code').click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(copied, block.querySelector('code').textContent);
  assert.equal(window.document.getElementById('toast').textContent, '代码已复制');
});

test('language tabs copy the selected example and JavaScript examples parse', async t => {
  let copied = '';
  const window = reader(t, { beforeParse(w) {
    Object.defineProperty(w.navigator, 'clipboard', { value: { writeText: async value => { copied = value; } } });
  }});
  const { document } = window;
  for (const code of document.querySelectorAll('pre[data-language="javascript"] code')) {
    assert.doesNotThrow(() => new vm.Script('(async () => {\n' + code.textContent + '\n})'));
  }
  const block = document.querySelector('.code-block:has(.code-tab)');
  block.querySelector('[data-language="python"]').click();
  assert.equal(block.querySelector('pre:not([hidden])').dataset.language, 'python');
  block.querySelector('.copy-code').click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(copied, block.querySelector('pre[data-language="python"] code').textContent);
  assert.match(copied, /urllib.request/);
  block.querySelector('.code-tab[aria-selected="true"]').focus();
  document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowLeft' }));
  assert.equal(block.querySelector('pre:not([hidden])').dataset.language, 'javascript');
});

test('offline clipboard fallback, failure feedback, menu, keyboard, and print', async t => {
  let prints = 0;
  const window = reader(t, { beforeParse(w) {
    w.print = () => { prints++; };
    w.document.execCommand = () => false;
  }});
  const { document } = window;
  document.querySelector('.copy-code').click();
  await new Promise(resolve => setImmediate(resolve));
  assert.match(document.getElementById('toast').textContent, /复制失败/);
  assert.equal(document.querySelector('textarea'), null);
  document.getElementById('menu').click();
  assert.equal(document.getElementById('menu').getAttribute('aria-expanded'), 'true');
  document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
  assert.equal(document.getElementById('menu').getAttribute('aria-expanded'), 'false');
  document.getElementById('menu').focus();
  document.dispatchEvent(new window.KeyboardEvent('keydown', { key: '/' }));
  assert.equal(document.activeElement.id, 'search');
  document.getElementById('print').click();
  assert.equal(prints, 1);
});
