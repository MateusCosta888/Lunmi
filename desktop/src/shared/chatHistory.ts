import type { ChatMessageDto } from './types';

export interface ConversationDto {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  temporary?: boolean;
  saved?: boolean;
}

export interface HistoryOptionsDto {
  enabled: boolean;
  startup: 'resume' | 'new';
}

export interface ChatHistorySnapshotDto {
  conversation: ConversationDto;
  messages?: ChatMessageDto[];
  has_more: boolean;
  before: number | null;
  options: HistoryOptionsDto;
  error: string | null;
}

export interface HistoryListDto {
  conversations: ConversationDto[];
  has_more: boolean;
}
