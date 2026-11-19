const fs = require('node:fs');
const crypto = require('node:crypto');
const vm = require('node:vm');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const filename = root+'/data/curriculum-gushici-candidates.json';
const payload = JSON.parse(fs.readFileSync(filename, 'utf8'));
const expected = [
    ['敕勒歌', 1135, '乐府诗集'],
    ['雪梅', 96, '卢梅坡'],
    ['太常引·建康中秋夜吕叔潜赋', 144, '辛弃疾'],
    ['墨梅', 52922, '王冕'],
];

(async () => {
    const library = await (await fetch('https://so.gushici.net/js/lz-string.min.js')).text();
    if (crypto.createHash('sha256').update(library).digest('hex') !==
        '95f4d1cbf099f57161b664bc048426ec3df92637801a4c79116e83315aa787e7') {
        throw Error('解压库内容已改变');
    }
    const context = {};
    vm.createContext(context);
    vm.runInContext(library, context, {timeout: 1000});
    for (const [requested, id, author] of expected) {
        const item = payload.items.find(row => row.title === requested);
        if (!item) throw Error('篇目不在缺失清单: '+requested);
        const response = await fetch(`https://so.gushici.net/data/data_${Math.floor(id/200)}.bin`);
        if (!response.ok) throw Error('来源请求失败: '+id);
        const data = JSON.parse(context.LZString.decompressFromEncodedURIComponent(await response.text()));
        const record = data[id];
        if (!record || record.a !== author ||
            (requested === '墨梅' && !record.c.startsWith('我家洗砚池头树'))) {
            throw Error('来源作品不匹配: '+requested);
        }
        const candidate = {site_id: id, title: record.t, author: record.a, dynasty: record.d,
            source_url: new URL(record.u, 'https://www.gushici.net').href, raw_text: record.c};
        item.matches = [candidate];
        item.manual_source_match = requested === '敕勒歌' || requested === '雪梅' || requested.startsWith('太常引');
        console.log(requested, candidate.source_url);
        await new Promise(resolve => setTimeout(resolve, 750));
    }
    const jiangnan = payload.items.find(item => item.title === '江南');
    jiangnan.matches = jiangnan.matches.filter(candidate => candidate.title === '江南');
    fs.writeFileSync(filename, JSON.stringify(payload, null, 2)+'\n');
})().catch(error => {console.error(error);process.exitCode = 1});
