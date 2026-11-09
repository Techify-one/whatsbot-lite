// What the contact list does when the server says a contact was deleted / archived / pinned by something
// other than this screen (another tab or device, the server itself). Pure: no DOM, no Preact.
// Tested with: node --test tests/contactListEvents.test.cjs
//
// Our own buttons already update the list locally, so applying the echo of those events must be a no-op.

function sortContacts(list) {
  return [...list].sort((a, b) => {
    const ap = a.is_pinned ? 1 : 0;
    const bp = b.is_pinned ? 1 : 0;
    if (ap !== bp) return bp - ap;
    return (b.last_message_ts || b.updated_at || 0) - (a.last_message_ts || a.updated_at || 0);
  });
}

/**
 * @param {Array} contacts      current list
 * @param {{type:string, phone:string, archived?:boolean, pinned?:boolean}} event
 * @param {{showArchived:boolean, selectedPhone:string|null}} view
 * @returns {{contacts:Array, deselect:boolean, refetch:boolean}}
 *   deselect: the open conversation went away, close it
 *   refetch:  the contact entered this view, ask the server to place it
 */
export function applyContactListEvent(contacts, event, view) {
  const none = { contacts, deselect: false, refetch: false };
  if (!event || !event.phone) return none;
  const { phone, type } = event;

  if (type === 'deleted') {
    return {
      contacts: contacts.filter(c => c.phone !== phone),
      deselect: view.selectedPhone === phone,
      refetch: false,
    };
  }
  if (type === 'archived') {
    const archived = !!event.archived;
    if (archived !== !!view.showArchived) {
      // It left the list being shown (inbox <-> archived).
      return {
        contacts: contacts.filter(c => c.phone !== phone),
        deselect: view.selectedPhone === phone,
        refetch: false,
      };
    }
    // It entered this view; only the server knows where it sorts.
    return { contacts, deselect: false, refetch: !contacts.some(c => c.phone === phone) };
  }
  if (type === 'pinned') {
    const pinned = !!event.pinned;
    if (!contacts.some(c => c.phone === phone && !!c.is_pinned !== pinned)) return none;
    return {
      contacts: sortContacts(contacts.map(c => (c.phone === phone ? { ...c, is_pinned: pinned } : c))),
      deselect: false,
      refetch: false,
    };
  }
  return none;
}
