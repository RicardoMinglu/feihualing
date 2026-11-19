// Collect only the missing curriculum works from the site's public search index.
const fs = require('node:fs');
const crypto = require('node:crypto');
const vm = require('node:vm');

const root = require('node:path').resolve(__dirname, '..');
const report = JSON.parse(fs.readFileSync(root+'/docs/current-stage-build-20261004.json', 'utf8'));
const output = root+'/data/curriculum-gushici-candidates.json';
const base = 'https://so.gushici.net';
const main = 'https://www.gushici.net';
const librarySha = '95f4d1cbf099f57161b664bc048426ec3df92637801a4c79116e83315aa787e7';
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const cache = new Map();
let lastFetch = 0;

async function retrieve(path) {
    if (cache.has(path)) return cache.get(path);
    const wait = 700 - (Date.now() - lastFetch);
    if (wait > 0) await delay(wait);
    let response;
    for (let attempt = 0; attempt < 3; attempt++) {
        response = await fetch(base+path, {headers: {'User-Agent': 'Feihualing curriculum review (selective public-domain poems)'}});
        lastFetch = Date.now();
        if (response.ok) break;
        if (![429, 503].includes(response.status)) throw Error(`${path}: HTTP ${response.status}`);
        await delay(2500 * (attempt+1));
    }
    if (!response.ok) throw Error(`${path}: HTTP ${response.status}`);
    const text = await response.text();
    cache.set(path, text);
    return text;
}

function normalize(text) {
    return String(text || '').normalize('NFKC').replace(/[^\p{Script=Han}]/gu, '');
}

function authorMatches(actual, expected) {
    if (normalize(actual) === normalize(expected)) return true;
    if (expected === '佚名' && ['佚名', '乐府诗集', '诗经'].some(value => normalize(actual).includes(value))) return true;
    return false;
}

async function mainTask() {
    const library = await retrieve('/js/lz-string.min.js');
    const digest = crypto.createHash('sha256').update(library).digest('hex');
    if (digest !== librarySha) throw Error('解压库内容已改变，请复核后继续');
    const context = {};
    vm.createContext(context);
    vm.runInContext(library, context, {timeout: 1000});
    async function decoded(path) {
        const key = 'json:'+path;
        if (cache.has(key)) return cache.get(key);
        const packed = await retrieve(path);
        const plain = context.LZString.decompressFromEncodedURIComponent(packed);
        if (!plain) throw Error(path+': 解压失败');
        const value = JSON.parse(plain);
        cache.set(key, value);
        return value;
    }
    async function candidates(query) {
        const chars = [...new Set(normalize(query))];
        const lists = [];
        for (const char of chars) {
            const list = await decoded('/data/idx_'+char.charCodeAt(0)+'.bin');
            if (!Array.isArray(list) || !list.length) return [];
            lists.push(list);
        }
        lists.sort((a, b) => a.length-b.length);
        const sets = lists.slice(1).map(list => new Set(list));
        return lists[0].filter(id => sets.every(set => set.has(id)));
    }
    const books = JSON.parse(fs.readFileSync(root+'/data/curriculum-current-books.json', 'utf8'));
    const bookMap = new Map(books.map(book => [book.id, book]));
    const collected = [];
    for (let index = 0; index < report.missing.length; index++) {
        const missing = report.missing[index];
        const book = bookMap.get(missing.book_id);
        const work = book?.works.find(item => item[0] === missing.title && item[1] === missing.author);
        if (!work) throw Error('目录条目不存在: '+missing.title);
        const title = normalize(missing.title);
        const firstLine = normalize(work[2]);
        const ids = await candidates(missing.title+missing.author);
        let newChunks = 0;
        const found = [];
        let firstFoundAt = null;
        for (const id of ids) {
            const path = '/data/data_'+Math.floor(id/200)+'.bin';
            if (!cache.has(path)) {
                if (newChunks >= 18 || (firstFoundAt !== null && newChunks-firstFoundAt >= 3)) break;
                newChunks++;
            }
            const item = (await decoded(path))[id];
            if (!item || !authorMatches(item.a, missing.author)) continue;
            const itemTitle = normalize(item.t);
            if (!itemTitle.includes(title) && !title.includes(itemTitle)) continue;
            if (firstLine && !normalize(item.c).includes(firstLine)) continue;
            if (firstFoundAt === null) firstFoundAt = newChunks;
            found.push({site_id: id, title: item.t, author: item.a, dynasty: item.d,
                source_url: new URL(item.u, main).href, raw_text: item.c});
            if (found.length >= 4) break;
        }
        collected.push({book_id: missing.book_id, volume: missing.volume,
            stage: book.stage, title: missing.title, author: missing.author,
            first_line: work[2] || null, candidate_ids: ids.length,
            chunks_fetched: newChunks, matches: found});
        console.log(`${index+1}/${report.missing.length} ${missing.title} ${missing.author}: ${found.length} matches (${newChunks} chunks)`);
    }
    const payload = {source: base, collected_at: new Date().toISOString(),
        report_sha256: crypto.createHash('sha256').update(fs.readFileSync(root+'/docs/current-stage-build-20261004.json')).digest('hex'),
        items: collected};
    fs.writeFileSync(output, JSON.stringify(payload, null, 2)+'\n');
    console.log(JSON.stringify({output, items: collected.length,
        found: collected.filter(item => item.matches.length).length, requests: [...cache.keys()].filter(key => !key.startsWith('json:')).length}));
}

mainTask().catch(error => {console.error(error);process.exitCode = 1});
