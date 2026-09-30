'use strict';
const $ = id => document.getElementById(id);
const state = {view:'overview', month:'', page:1, vendor:'', status:'', category:'', generation:0};
const titles = {overview:['Your month, at a glance.','A clear view of the receipts you’ve recorded.'], receipts:['Every receipt, in one place.','Find the details, make a correction, or revisit the original.'], review:['A second look, when it matters.','Confirm the details before a receipt joins your spending totals.'], vendors:['Familiar names. Fewer questions.','Manage the vendor names and categories your bot has learned.']};
const money = value => Number(value).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const readableDate = value => new Date(value+'T00:00:00Z').toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'});
function el(tag, cls='', text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
function button(text, fn, cls=''){const node=el('button',cls,text);node.type='button';node.addEventListener('click',()=>run(node,fn));return node;}
function notice(text,error=false){$('notice').textContent=text;$('notice').className=error?'error':'';$('notice').hidden=false;}
function problem(error){if($('detail').open){let box=$('detail-content').querySelector('.dialog-error');if(!box){box=el('p','dialog-error');$('detail-content').prepend(box);}box.textContent=error.message;}else if(!$('workspace').hidden)notice(error.message,true);else $('login-message').textContent=error.message;}
async function run(node,fn){node.disabled=true;try{await fn();}catch(error){problem(error);}finally{node.disabled=false;}}
async function api(path, body){const response=await fetch('/api/dashboard'+path,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:body===undefined?{}:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),cache:'no-store'});const data=await response.json();if(!response.ok){if(response.status===401){$('detail').close();$('workspace').hidden=true;$('login').hidden=false;}const error=new Error(typeof data.detail==='string'?data.detail:'Please check the values and try again.');error.status=response.status;throw error;}return data;}
function empty(title,text){const box=el('div','empty');box.append(el('strong','',title),el('span','',text));return box;}
function card(title,subtitle){const box=el('section','card'),head=el('div','card-head'),copy=el('div');copy.append(el('h2','',title));if(subtitle)copy.append(el('p','',subtitle));head.append(copy);box.append(head);return box;}
function pill(status){return el('span','badge '+(status==='COMPLETED'?'completed':status==='NEEDS_REVIEW'||status==='PENDING_CATEGORY'?'review':''),status.replaceAll('_',' ').toLowerCase());}
function receiptTable(items,compact=false){if(!items.length)return empty('No receipts here yet.','Send a photo to your Telegram bot, or try another month or filter.');const wrap=el('div',compact?'table-wrap compact-table':'table-wrap'),table=el('table'),head=el('thead'),tr=el('tr');(compact?['Vendor','Total','']:['Vendor','Date','Category','Total','Status','']).forEach(x=>tr.append(el('th','',x)));head.append(tr);table.append(head);const body=el('tbody');for(const r of items){const row=el('tr'),vendor=el('td','vendor-cell',r.vendor);vendor.title=r.vendor;row.append(vendor);if(!compact)row.append(el('td','muted',readableDate(r.date)),el('td','',r.category||'Unassigned'));row.append(el('td','amount',money(r.total)));const status=el('td');status.append(pill(r.status));const action=el('td');action.append(button('Open ↗',()=>openReceipt(r.id),'text-button'));if(!compact)row.append(status);row.append(action);body.append(row);}table.append(body);wrap.append(table);return wrap;}
function pager(container,more){const box=el('div','pager'),prev=button('← Previous',async()=>{state.page--;await load();}),next=button('Next →',async()=>{state.page++;await load();});prev.disabled=state.page===1;next.disabled=!more;box.append(prev,el('span','',`Page ${state.page}`),next);container.append(box);}
async function overview(){const [summary,recent]=await Promise.all([api('/summary?month='+state.month),api('/receipts?month='+state.month+'&status=COMPLETED')]);const result=el('div'),stats=el('div','stats');const values=[['Total recorded',money(summary.total),'Completed receipts this month'],['Receipts',String(summary.count),'Recorded and ready'],['Average receipt',money(summary.average),'Per completed receipt'],['Awaiting review',String(summary.pending),'Across all receipt dates']];for(const [label,value,note]of values){const item=el('div','stat');item.append(el('div','stat-label',label),el('div','stat-value',value),el('div','stat-note',note));stats.append(item);}result.append(stats);const columns=el('div','two-col'),recentCard=card('Recent receipts','Your latest completed receipts in this month');recentCard.append(receiptTable(recent.items.slice(0,5),true));const side=card('Where it went','Spending by saved category'),body=el('div','card-body');if(!summary.categories.length)body.append(empty('A fresh start.','Your category breakdown will appear here.'));for(const c of summary.categories){const row=el('div','category-row'),info=el('div','category-info'),progress=el('progress');progress.max=Number(summary.total)||1;progress.value=Number(c.total);progress.setAttribute('aria-label',`${c.name}: ${money(c.total)}`);info.append(el('span','',c.name),el('strong','',money(c.total)));row.append(info,progress);body.append(row);}const callout=el('div','callout');callout.append(el('h3','',summary.pending?'A little attention goes a long way.':'All caught up.'),el('p','',summary.pending?'Check receipts that need a confirmed date or category. They stay out of totals until completed.':'No receipts are waiting for review. Send your next receipt to Telegram whenever you’re ready.'),button('Go to review queue →',()=>navigate('review'),'text-button'));body.append(callout);side.append(body);columns.append(recentCard,side);result.append(columns);return result;}
async function receiptsView() {
  const query = new URLSearchParams({month: state.month, page: state.page});
  if (state.vendor) query.set('vendor', state.vendor);
  if (state.status) query.set('status', state.status);
  if (state.category) query.set('category', state.category);
  const data = await api('/receipts?' + query);
  const box = card('Receipt library', 'Filtered by receipt date. Open a receipt to see its image and history.');
  const form = el('form', 'filters');
  const search = el('input'), status = el('select'), category = el('select');
  search.placeholder = 'Search a vendor…';
  search.value = state.vendor;
  search.maxLength = 200;
  search.setAttribute('aria-label', 'Search vendor');
  for (const [value, label] of [['', 'All statuses'], ['COMPLETED', 'Completed'], ['NEEDS_REVIEW', 'Needs review'], ['PENDING_CATEGORY', 'Needs category'], ['FAILED', 'Failed']]) {
    const opt = el('option', '', label);
    opt.value = value;
    status.append(opt);
  }
  status.value = state.status;
  const all = el('option', '', 'All categories');
  all.value = '';
  category.append(all);
  const categories = [...data.categories];
  // Keep a selected category visible after its last receipt is corrected.
  if (state.category && !categories.includes(state.category)) categories.push(state.category);
  for (const name of categories) {
    const opt = el('option', '', name);
    opt.value = name;
    category.append(opt);
  }
  category.value = state.category;
  const vendorLabel = el('label', '', 'Vendor');
  const categoryLabel = el('label', '', 'Category');
  const statusLabel = el('label', '', 'Status');
  vendorLabel.append(search);
  categoryLabel.append(category);
  statusLabel.append(status);
  const submit = el('button', '', 'Apply filters');
  submit.type = 'submit';
  form.append(vendorLabel, categoryLabel, statusLabel, submit);
  form.addEventListener('submit', event => {
    event.preventDefault();
    run(submit, async () => {
      state.vendor = search.value;
      state.category = category.value;
      state.status = status.value;
      state.page = 1;
      await load();
    });
  });
  box.append(form, receiptTable(data.items));
  pager(box, data.has_more);
  return box;
}
async function reviewView(){const data=await api('/review'),box=card('Review queue','Finish a review here or continue in Telegram.');if(!data.items.length)box.append(empty('You’re all caught up.','Receipts that need your input will appear here.'));for(const r of data.items){const row=el('div','review-card'),text=el('div');text.append(pill(r.status),el('h3','',r.vendor),el('p','',`${readableDate(r.date)} · ${money(r.total)} · ${r.category||'Category needed'}`));row.append(text,button('Review receipt →',()=>openReceipt(r.id),'primary'));box.append(row);}return box;}
async function vendorsView(){const data=await api('/vendors?page='+state.page),group=el('div'),columns=el('div','two-col'),box=card('Learned vendors','Categories used for future receipts');if(!data.items.length)box.append(empty('No learned vendors yet.','Assign a category to a receipt in Telegram to get started.'));for(const v of data.items){const row=el('div','alias-row');row.append(el('strong','',v.name),el('span','',v.category));box.append(row);}const aliases=card('Alternate names','Explicit links, without fuzzy matching');for(const a of data.aliases){const row=el('div','alias-row'),copy=el('div');copy.append(el('strong','',a.name),el('p','small',`Linked to ${a.target}`));row.append(copy,button('Remove',async()=>{if(!confirm(`Remove the alias “${a.name}”? Saved receipts will stay unchanged.`))return;await api('/aliases',{action:'remove',name:a.name});await load();notice('Alias removed.');},'text-button'));aliases.append(row);}if(!data.aliases.length)aliases.append(empty('No aliases on this page.','Link an alternate spelling to a learned vendor below.'));columns.append(box,aliases);group.append(columns);const add=card('Link an alternate name','Future receipts with this exact name will use the target’s learned category.'),form=el('form','alias-form'),name=el('input'),target=el('select'),l1=el('label','','Alternate name'),l2=el('label','','Learned vendor');name.placeholder='e.g. Starbucks #1234';name.required=true;name.maxLength=200;const placeholder=el('option','','Choose a vendor');placeholder.value='';target.append(placeholder);target.required=true;for(const v of data.items){const opt=el('option','',v.name);opt.value=v.name;target.append(opt);}l1.append(name);l2.append(target);const submit=el('button','primary','Save alias');submit.type='submit';submit.disabled=!data.items.length;form.append(l1,l2,submit);form.addEventListener('submit',e=>{e.preventDefault();run(submit,async()=>{await api('/aliases',{action:'add',name:name.value,target:target.value});await load();notice('Alias saved. Existing receipts are unchanged.');});});add.append(form);group.append(add);pager(group,data.has_more);return group;}
async function load(){const generation=++state.generation;$('content').replaceChildren(el('div','loading','Loading your workspace…'));try{const node=await ({overview,receipts:receiptsView,review:reviewView,vendors:vendorsView}[state.view])();if(generation===state.generation)$('content').replaceChildren(node);}catch(error){if(generation===state.generation){$('content').replaceChildren(empty('Couldn’t load this view.','Check your connection and try again.'));$('content').append(button('Try again',load));problem(error);}}}
async function navigate(view){state.view=view;state.page=1;document.querySelectorAll('nav button').forEach(b=>{b.classList.toggle('active',b.dataset.view===view);b.setAttribute('aria-current',b.dataset.view===view?'page':'false');});$('page-title').textContent=titles[view][0];$('page-subtitle').textContent=titles[view][1];$('breadcrumb').textContent=view==='review'?'Review queue':view[0].toUpperCase()+view.slice(1);$('month-label').hidden=['review','vendors'].includes(view);$('notice').hidden=true;await load();}
function field(label,value){const node=el('label','',label);node.append(el('strong','',value??'Not shown'));return node;}
async function openReceipt(id){const [r,options]=await Promise.all([api('/receipts/'+id),api('/category-options')]);const body=$('detail-content');body.replaceChildren();const suggestions=el('datalist');suggestions.id='category-suggestions';for(const name of options.categories){const option=el('option');option.value=name;suggestions.append(option);}body.append(suggestions);$('detail-title').textContent=r.vendor;const details=el('div','detail-grid');details.append(field('Receipt date',readableDate(r.date)),field('Total',money(r.total)),field('Category',r.category||'Unassigned'),field('VAT',r.vat===null?'Not shown':money(r.vat)),field('Status',r.status.replaceAll('_',' ')),field('Extraction confidence',r.confidence));body.append(details,el('p','detail-id',r.id));const imageButton=button('View receipt image ↗',async()=>{const data=await api(`/receipts/${id}/image`);const image=el('img','receipt-image');image.alt='Original receipt from '+r.vendor;image.referrerPolicy='no-referrer';image.src=data.url;image.addEventListener('error',()=>{image.remove();problem(new Error('Could not load the receipt image. The link may have expired; try again.'));});body.querySelector('.receipt-image')?.remove();body.append(image);});body.append(imageButton);
 if(r.status==='COMPLETED'){const form=el('form','detail-form'),select=el('select'),value=el('input'),l1=el('label','','Correct field'),l2=el('label','','New value');for(const [key,label]of [['category','Category'],['total','Total'],['date','Date'],['vendor','Vendor']]){const option=el('option','',label);option.value=key;select.append(option);}const setValue=()=>{value.type=select.value==='date'?'date':'text';if(select.value==='category')value.setAttribute('list','category-suggestions');else value.removeAttribute('list');value.value=r[select.value]||'';value.maxLength=select.value==='vendor'?200:100;};select.addEventListener('change',setValue);setValue();value.required=true;l1.append(select);l2.append(value);const save=el('button','primary','Save change');save.type='submit';form.append(l1,l2,save);form.addEventListener('submit',e=>{e.preventDefault();run(save,async()=>{await api(`/receipts/${id}/edit`,{field:select.value,value:value.value});$('detail').close();await load();notice('Receipt corrected. The change is recorded in its history.');});});body.append(form);}
 if(r.status==='NEEDS_REVIEW'||r.status==='PENDING_CATEGORY'){const form=el('form','detail-form'),input=el('input'),label=el('label','',r.status==='NEEDS_REVIEW'?'Confirm receipt date':'Expense category');if(r.status==='NEEDS_REVIEW'){input.type='date';input.value=r.date;body.append(el('p','small',`Printed date: ${r.raw_date||'Unknown'}. ${r.review_reason?r.review_reason.replaceAll('_',' ').replaceAll(',',' · '):'Please check the receipt details.'}`));}else{input.placeholder='Choose or type a category';input.setAttribute('list','category-suggestions');input.maxLength=100;}input.required=true;label.append(input);const save=el('button','primary',r.status==='NEEDS_REVIEW'?'Confirm details':'Save category');save.type='submit';form.append(label,save);form.addEventListener('submit',e=>{e.preventDefault();run(save,async()=>{await api(`/receipts/${id}/review`,{action:r.status==='NEEDS_REVIEW'?'confirm':'category',value:input.value});$('detail').close();await load();notice('Receipt updated. Check the queue for any remaining category step.');});});body.append(form);if(r.status==='NEEDS_REVIEW')body.append(button('Discard draft and retry in Telegram',async()=>{if(!confirm('Discard this unconfirmed draft? You will need to send a new receipt photo in Telegram.'))return;await api(`/receipts/${id}/review`,{action:'retry'});$('detail').close();await load();notice('Draft discarded. Send a clearer photo to Telegram.');},'danger'));}
 let historyPage=1;const history=el('pre','history'),historyButton=button('Show change history',async()=>{const data=await api(`/receipts/${id}/history?page=${historyPage}`);history.textContent=data.text;historyPage++;historyButton.textContent='Show older history';});body.append(el('hr'),historyButton,history);if(!$('detail').open)$('detail').showModal();}
async function boot(){const fragment=new URLSearchParams(location.hash.slice(1)),token=fragment.get('token');if(token){history.replaceState(null,'',location.pathname);$('login-message').textContent='Signing you in securely…';await api('/login',{token});}let session;try{session=await api('/session');}catch(error){if(error.status===401){$('login-message').textContent='Use your Telegram bot to sign in. No new password to remember.';return;}throw error;}state.month=session.month;$('month').value=state.month;$('login').hidden=true;$('workspace').hidden=false;await navigate('overview');}
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>run(b,()=>navigate(b.dataset.view))));$('month').addEventListener('change',()=>{if(!$('month').value)return;state.month=$('month').value;state.page=1;load();});$('close-detail').addEventListener('click',()=>$('detail').close());$('retry-session').addEventListener('click',()=>run($('retry-session'),boot));$('logout').addEventListener('click',()=>run($('logout'),async()=>{await api('/logout',{});$('workspace').hidden=true;$('login').hidden=false;$('content').replaceChildren();$('login-message').textContent='You’re signed out. Send /dashboard to your Telegram bot for a new sign-in link.';}));$('export').addEventListener('click',()=>run($('export'),async()=>{const response=await fetch('/api/dashboard/export?period='+encodeURIComponent(state.month),{credentials:'same-origin',cache:'no-store'});if(!response.ok){const data=await response.json();throw new Error(typeof data.detail==='string'?data.detail:'Export failed. Please sign in again or try a different month.');}const url=URL.createObjectURL(await response.blob()),link=el('a');link.href=url;link.download=`expenses_${state.month}.csv`;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);notice('CSV exported for '+state.month+'.');}));boot().catch(problem);

async function loadSignInOptions() {
  try {
    const config = await api('/public-config');
    if (config.telegram_url && /^https:\/\/t\.me\/[A-Za-z][A-Za-z0-9_]{4,31}$/.test(config.telegram_url)) {
      $('open-telegram').href = config.telegram_url;
      $('open-telegram').hidden = false;
    }
  } catch { /* The manual Telegram steps remain available. */ }
}
$('copy-command').addEventListener('click', () => run($('copy-command'), async () => {
  try {
    await navigator.clipboard.writeText('/dashboard');
    $('copy-status').textContent = 'Copied! Paste it into your private bot chat.';
  } catch {
    $('copy-status').textContent = 'Copy this command manually: /dashboard';
  }
}));
loadSignInOptions();
