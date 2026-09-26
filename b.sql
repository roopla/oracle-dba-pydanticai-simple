
create table block_demo (
       id number primary key,
       name varchar2(50)
     );

insert into block_demo values (1,'Oracle');

commit;

update block_demo
     set name='Blocking Session'
     where id=1;





update block_demo
     set name='Blocked Session'
     where id=1;
