async page=>{
 await page.goto('http://127.0.0.1:8765');await page.evaluate(()=>localStorage.removeItem('feihualing.game'));await page.reload();
 const checks=[];
 for(const width of [360,390,430]){
  await page.setViewportSize({width,height:844});
  for(const size of [36,48]){
   await page.locator('.word-input').evaluate((e,n)=>e.style.fontSize=n+'px',size);
   for(const word of ['山','月','风','鬱']){
    await page.getByRole('textbox',{name:'飞花令字'}).fill(word);
    const good=await page.locator('.word-input').evaluate(e=>{const css=getComputedStyle(e),font=parseFloat(css.fontSize);return e.clientWidth-parseFloat(css.paddingLeft)-parseFloat(css.paddingRight)>font&&e.clientHeight>font*1.5&&e.scrollWidth<=e.clientWidth});
    if(!good)throw Error('clipping '+width+size+word);checks.push(width+'/'+size+'/'+word);
   }
  }
 }
 await page.setViewportSize({width:390,height:844});await page.getByRole('textbox',{name:'飞花令字'}).fill('山');await page.mouse.move(0,0);await page.screenshot({path:'output/playwright/v130/webkit/enlarged-word.png'});return {count:checks.length,checks};
}
