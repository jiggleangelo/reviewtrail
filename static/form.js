document.querySelectorAll('textarea[maxlength]').forEach(x=>x.addEventListener('input',()=>x.title=`${x.value.length}/${x.maxLength} characters`));
