([guid, icon, x, y]) => {
    if (network_guid !== guid) throw Error('The editor changed.');
    const canvas = $('#network_scheme'), item = $(document.getElementById(icon));
    const drop = canvas.droppable('option', 'drop');
    if (!item.hasClass('drag') || typeof drop !== 'function')
        throw Error('This palette item is unavailable.');
    const box = canvas[0].getBoundingClientRect();
    const position = {left: box.left + scrollX + box.width*x/100,
                      top: box.top + scrollY + box.height*y/100};
    drop.call(canvas[0], $.Event('drop'), {draggable: item, position});
}
