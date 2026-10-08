(function(){const setText=(el,v)=>{if(el)el.textContent=v==null?"":String(v)};const f=document.querySelector('[data-user-filter-form]');if(!f)return;
const search=f.querySelector('#user-filter-search'),status=f.querySelector('[data-filter-status]'),sort=f.querySelector('[data-filter-sort]'),order=f.querySelector('[data-filter-order]');
const list=document.querySelector('.user-list'),pager=document.querySelector('[data-user-pagination]');
if(!list||!pager)return;const previous=pager.querySelector('[data-page-prev]'),next=pager.querySelector('[data-page-next]'),pageLabel=pager.querySelector('[data-page-label]');
const all=Array.from(document.querySelectorAll('.user-row')),pageSize=50;
const initialParams=new URLSearchParams(location.search),fromDay=initialParams.get('registered_from')||'',toDay=initialParams.get('registered_to')||'';
let currentPage=Number(pager.dataset.initialPage)||1;
const norm=v=>String(v??'').trim().toLocaleLowerCase('ru-RU');
const apply=()=>{
 const query=norm(search.value),st=status.value,so=sort.value,od=order.value;
 const filtered=all.filter(row=>{
  const matchesQuery=!query||norm(row.dataset.userSearch).includes(query);
  const matchesStatus=st==='all'||(st==='online'&&row.dataset.userOnline==='1')||row.dataset.userStatus===st;
  const day=row.dataset.userRegistered||'';return matchesQuery&&matchesStatus&&(!fromDay||(day&&day>=fromDay))&&(!toDay||(day&&day<=toDay));
 });
 const keys={remaining:'userRemaining',last_online:'userLastOnline',traffic:'userTraffic',quota:'userQuota',name:'userName',registration:'userRegistered'};
 const key=keys[so]||keys.remaining;
 filtered.sort((a,b)=>{
  let cmp=0;
  if(so==='name') cmp=norm(a.dataset[key]).localeCompare(norm(b.dataset[key]),'ru-RU',{numeric:true,sensitivity:'base'});
  else if(so==='registration') cmp=String(a.dataset[key]||'9999-99-99').localeCompare(String(b.dataset[key]||'9999-99-99'));
  else {const av=Number(a.dataset[key])||0,bv=Number(b.dataset[key])||0;cmp=av===bv?0:(av<bv?-1:1);}
  if(cmp===0) cmp=(Number(a.dataset.userTgId)||0)-(Number(b.dataset.userTgId)||0);
  return cmp*(od==='desc'?-1:1);
 });
 const pages=Math.max(1,Math.ceil(filtered.length/pageSize));
 currentPage=Math.min(Math.max(1,currentPage),pages);
 const startIndex=(currentPage-1)*pageSize;
 const visibleSet=new Set(filtered.slice(startIndex,startIndex+pageSize));
 all.forEach(row=>{row.hidden=!visibleSet.has(row);});
 const frag=document.createDocumentFragment();
 filtered.forEach(row=>frag.appendChild(row));
 list.replaceChildren(frag);if(!filtered.length){const empty=document.createElement('div');empty.className='card';empty.textContent='Пользователи не найдены';list.appendChild(empty);}
 setText(document.getElementById('users-visible-count'),filtered.length);
 setText(document.getElementById('users-total-count'),all.length);
 pageLabel.textContent='Страница '+currentPage+' из '+pages;
 previous.disabled=currentPage<=1; next.disabled=currentPage>=pages;
 const params=new URLSearchParams();if(fromDay)params.set('registered_from',fromDay);if(toDay)params.set('registered_to',toDay);if(search.value.trim())params.set('q',search.value.trim());if(st!=='all')params.set('status',st);if(so!=='remaining')params.set('sort',so);if(od!=='asc')params.set('order',od);if(currentPage>1)params.set('page_number',currentPage);
 if(location.origin!=='null')history.replaceState(null,'',location.pathname+(params.size?'?'+params:''));
};
const reset=()=>{currentPage=1;apply();};let timer=0;search.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(reset,120);});
[status,sort,order].forEach(el=>el.addEventListener('change',reset));
previous.addEventListener('click',()=>{currentPage--;apply();});next.addEventListener('click',()=>{currentPage++;apply();});
window.addEventListener('popstate',()=>location.reload());apply();})();
