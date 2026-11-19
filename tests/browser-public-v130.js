async page=>{
 const base='https://feihualing.ltkeep.cn',checks=[],errors=[];page.on('pageerror',e=>errors.push(e.message));const check=(n,v)=>{if(!v)throw Error(n);checks.push(n)};
 await page.goto(base);await page.evaluate(()=>localStorage.removeItem('feihualing.game'));await page.reload();await page.setViewportSize({width:390,height:844});
 await page.getByRole('button',{name:'界面主题'}).click();await page.getByRole('radio',{name:'晴山诗会',exact:true}).check();await page.getByRole('button',{name:'完成',exact:true}).click();
 await page.locator('[data-level="endless"]').click();await page.getByRole('textbox',{name:'飞花令字'}).fill('山');
 check('word fits',await page.locator('.word-input').evaluate(e=>e.clientWidth===e.scrollWidth));await page.mouse.move(0,0);await page.screenshot({path:'output/playwright/v130/public/home.png'});
 await page.getByRole('button',{name:'开始对战',exact:true}).click();await page.waitForURL('**#battle');await page.getByRole('textbox',{name:'输入诗句'}).fill('空山新雨后');await page.getByRole('button',{name:'提交诗句'}).click();await page.locator('.poem-message.computer').waitFor();
 check('unlimited live',(await page.locator('.battle-meta').innerText()).includes('不限时')&&(await page.locator('.hint-btn').innerText()).includes('不限次'));
 check('chat direction',await page.locator('#history').evaluate(e=>e.querySelector('.player').getBoundingClientRect().left>e.querySelector('.computer').getBoundingClientRect().left));await page.mouse.move(0,0);await page.screenshot({path:'output/playwright/v130/public/battle.png'});
 await page.getByRole('button',{name:'退出对局'}).click();await page.getByRole('button',{name:'离开对局',exact:true}).click();await page.waitForURL('**#home');
 await page.getByRole('link',{name:'查诗',exact:true}).click();await page.locator('.result-item').first().waitFor();await page.getByRole('button',{name:'查看更多',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.result-item').length===40);
 await page.locator('#search-results').evaluate(e=>e.scrollTop=e.scrollHeight);check('footer visible after more',await page.locator('[data-action="search-start"]').evaluate(e=>e.getBoundingClientRect().bottom<=innerHeight));
 await page.locator('#search-results').evaluate(e=>e.scrollTop=0);await page.mouse.move(0,0);await page.screenshot({path:'output/playwright/v130/public/search.png'});
 await page.getByRole('button',{name:'作者',exact:true}).click();await page.getByRole('textbox',{name:'作者姓名'}).fill('李白');await page.getByRole('button',{name:'查诗',exact:true}).click();await page.locator('.result-item').first().waitFor();check('author works',(await page.locator('.results-bar').innerText()).includes('首作品'));await page.screenshot({path:'output/playwright/v130/public/author.png'});
 await page.getByRole('button',{name:'诗句',exact:true}).click();await page.getByRole('textbox',{name:'诗句关键词'}).fill('明月光疑是地上霜');await page.getByRole('button',{name:'查诗',exact:true}).click();await page.locator('.result-item').first().waitFor();check('phrase search',(await page.locator('.result-title').innerText())==='静夜思');await page.screenshot({path:'output/playwright/v130/public/phrase.png'});
 check('no script errors',errors.length===0);return {checks,errors};
}
