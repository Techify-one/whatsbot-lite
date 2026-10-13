import { useEffect } from 'preact/hooks';
import { createWebSocket } from '../services/websocket.js';

export function useWebSocket({ onStatus, onQrUpdate, onGowaStatus, onConfigSaved, onNewMessage, onChatPresence, onContactInfoUpdated, onTagsChanged, onContactTagsUpdated, onHumanTransferAlert, onContactAiToggled, onMessagesRead, onMessageStatus, onMessageAction, onMessageReaction, onMediaUpdated, onAvatarUpdated, onGroupParticipantsChanged, onLowBalance, onGowaUpdateAvailable, onGowaUpdateProgress, onGowaUpdateDone, onWsConnect, onWsDisconnect }) {
  useEffect(() => {
    const ws = createWebSocket({
      onConnect: onWsConnect,
      onDisconnect: onWsDisconnect,
      status: onStatus,
      qr_update: onQrUpdate,
      gowa_status: onGowaStatus,
      config_saved: onConfigSaved,
      new_message: onNewMessage,
      chat_presence: onChatPresence,
      contact_info_updated: onContactInfoUpdated,
      tags_changed: onTagsChanged,
      contact_tags_updated: onContactTagsUpdated,
      human_transfer_alert: onHumanTransferAlert,
      contact_ai_toggled: onContactAiToggled,
      messages_read: onMessagesRead,
      message_status: onMessageStatus,
      message_revoked: onMessageAction ? (d) => onMessageAction({ ...d, action: 'revoked' }) : undefined,
      message_deleted: onMessageAction ? (d) => onMessageAction({ ...d, action: 'deleted' }) : undefined,
      message_reaction: onMessageReaction,
      message_media_updated: onMediaUpdated,
      avatar_updated: onAvatarUpdated,
      group_participants_changed: onGroupParticipantsChanged,
      low_balance: onLowBalance,
      gowa_update_available: onGowaUpdateAvailable,
      gowa_update_progress: onGowaUpdateProgress,
      gowa_update_done: onGowaUpdateDone,
    });
    return () => ws.close();
  }, []);
}
