'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import {
  Bot,
  Clock3,
  DatabaseZap,
  LayoutDashboard,
  LayoutPanelTop,
  MessageSquarePlus,
  Settings,
} from 'lucide-react';
import { toast } from 'sonner';
import { ChatHistoryList } from '@/components/chat-sidebar/chat-history-list';
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarSeparator,
  useSidebar,
} from '@/components/ui/sidebar';
import { APP_VERSION } from '@/lib/app-version';
import {
  fetchChatList,
  getCachedChatList,
  removeCachedChat,
  replaceCachedChatList,
  subscribeToChatListChanges,
  type ChatSummary,
  upsertCachedChat,
} from '@/lib/chat-list-cache';
import { Spinner } from '@/components/ui/spinner';
import { WorkspaceSwitcher } from '@/components/workspace-switcher';

type Chat = ChatSummary;

interface AppSidebarProps {
  activeChatId?: string;
  onNewChat?: () =>
    | ChatSummary
    | null
    | void
    | Promise<ChatSummary | null | void>;
  refreshToken?: number;
}

export function AppSidebar({
  activeChatId,
  onNewChat,
  refreshToken = 0,
}: AppSidebarProps) {
  const pathname = usePathname();
  const router = useRouter();
  const isOnChat = pathname.startsWith('/chat');
  const [chatList, setChatList] = useState<Chat[]>(
    () => getCachedChatList() ?? [],
  );
  const [isLoading, setIsLoading] = useState(
    () => getCachedChatList() === null,
  );
  const [isCreatingChat, setIsCreatingChat] = useState(false);
  const [deletingChatId, setDeletingChatId] = useState<string | null>(null);
  const [editingChatId, setEditingChatId] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState('');
  const [savingChatId, setSavingChatId] = useState<string | null>(null);
  const { isMobile, setOpenMobile } = useSidebar();

  const fetchChats = useCallback(async (options: { force?: boolean } = {}) => {
    if (options.force || getCachedChatList() === null) {
      setIsLoading(true);
    }

    try {
      setChatList(await fetchChatList(options));
    } catch (error) {
      console.error('Failed to fetch chats', error);
      toast.error('Could not load chat history.');
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchChats({ force: refreshToken > 0 });
  }, [fetchChats, refreshToken]);

  useEffect(() => {
    const cachedChats = getCachedChatList();
    if (cachedChats) {
      setChatList(cachedChats);
    }
  }, [activeChatId]);

  useEffect(
    () =>
      subscribeToChatListChanges(() => {
        const cachedChats = getCachedChatList();
        if (cachedChats) {
          setChatList(cachedChats);
        }
      }),
    [],
  );

  const closeMobileSidebar = useCallback(() => {
    if (isMobile) {
      setOpenMobile(false);
    }
  }, [isMobile, setOpenMobile]);

  const handleDelete = async (chatId: string) => {
    const previousChatList = chatList;
    const deletingActiveChat = chatId === activeChatId;
    const remainingChats = removeCachedChat(chatId);

    setDeletingChatId(chatId);
    setEditingChatId((current) => (current === chatId ? null : current));
    setChatList(remainingChats);

    try {
      const response = await fetch(`/api/chats/${chatId}`, {
        method: 'DELETE',
      });
      if (!response.ok) {
        throw new Error('Failed to delete chat');
      }
    } catch (error) {
      console.error('Failed to delete chat', error);
      replaceCachedChatList(previousChatList);
      setChatList(previousChatList);
      toast.error('Could not delete chat.');
      setDeletingChatId((current) => (current === chatId ? null : current));
      return;
    }

    if (deletingActiveChat) {
      try {
        const replacementChatId = remainingChats[0]?.id;

        if (replacementChatId) {
          router.push(`/chat/${replacementChatId}`);
        } else {
          router.push('/chat');
        }
        closeMobileSidebar();
      } catch (error) {
        console.error('Deleted chat but failed to open a replacement', error);
        toast.error('Chat deleted, but opening the next chat failed.');
        router.push('/chat');
      }
    }

    setDeletingChatId((current) => (current === chatId ? null : current));
  };

  const handleSelectChat = (id: string) => {
    router.push(`/chat/${id}`);
    closeMobileSidebar();
  };

  const handleNewChat = async () => {
    if (isCreatingChat) {
      return;
    }

    setIsCreatingChat(true);
    setEditingChatId(null);

    if (onNewChat) {
      try {
        const chat = await onNewChat();
        if (!chat) {
          throw new Error('Failed to create chat');
        }
        setChatList(upsertCachedChat(chat));
        closeMobileSidebar();
      } catch (error) {
        console.error('Failed to create chat', error);
        toast.error('Could not create chat.');
      } finally {
        setIsCreatingChat(false);
      }
    } else {
      // Fallback: create a chat and navigate
      try {
        const response = await fetch('/api/chats', { method: 'POST' });
        if (!response.ok) throw new Error('Failed to create chat');
        const chat = (await response.json()) as Chat;
        setChatList(upsertCachedChat(chat));
        router.push(`/chat/${chat.id}`);
        closeMobileSidebar();
      } catch (error) {
        console.error('Failed to create chat', error);
        toast.error('Could not create chat.');
      } finally {
        setIsCreatingChat(false);
      }
    }
  };

  const handleStartRename = useCallback((chat: Chat) => {
    setEditingChatId(chat.id);
    setEditingTitle(chat.title);
  }, []);

  const handleCancelRename = useCallback(() => {
    setEditingChatId(null);
    setEditingTitle('');
  }, []);

  const handleRename = useCallback(
    async (chatId: string) => {
      const title = editingTitle.trim().slice(0, 50);
      if (!title) {
        toast.error('Chat title cannot be empty.');
        return;
      }

      setSavingChatId(chatId);

      try {
        const response = await fetch(`/api/chats/${chatId}`, {
          method: 'PATCH',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ title, force: true }),
        });
        if (!response.ok) {
          throw new Error('Failed to rename chat');
        }

        const updatedChat = (await response.json()) as Chat;
        setChatList(upsertCachedChat(updatedChat));
        setEditingChatId(null);
        setEditingTitle('');
      } catch (error) {
        console.error('Failed to rename chat', error);
        toast.error('Could not rename chat.');
      } finally {
        setSavingChatId((current) => (current === chatId ? null : current));
      }
    },
    [editingTitle],
  );

  return (
    <Sidebar variant="floating" collapsible="icon">
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton asChild size="lg" tooltip="WoTBot">
              <Link href="/">
                <div className="flex size-8 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
                  <Bot className="size-4" />
                </div>
                <div className="grid flex-1 text-left text-sm leading-tight group-data-[collapsible=icon]:hidden">
                  <span className="truncate text-base font-semibold">
                    WoTBot
                  </span>
                  <span
                    className="truncate text-xs"
                    title={`Build ${APP_VERSION}`}
                  >
                    {APP_VERSION}
                  </span>
                </div>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
        <WorkspaceSwitcher />
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Catalog</SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  asChild
                  isActive={pathname.startsWith('/sources')}
                  tooltip="Sources"
                >
                  <Link href="/sources">
                    <DatabaseZap />
                    <span>Sources</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton
                  asChild
                  isActive={pathname.startsWith('/things')}
                  tooltip="Things"
                >
                  <Link href="/things">
                    <LayoutDashboard />
                    <span>Things</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton
                  asChild
                  isActive={pathname.startsWith('/jobs')}
                  tooltip="Jobs"
                >
                  <Link href="/jobs">
                    <Clock3 />
                    <span>Jobs</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton
                  asChild
                  isActive={pathname.startsWith('/panels')}
                  tooltip="Panels"
                >
                  <Link href="/panels">
                    <LayoutPanelTop />
                    <span>Panels</span>
                  </Link>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup>
          <SidebarGroupLabel>Chat</SidebarGroupLabel>
          <SidebarMenuItem>
            <SidebarMenuButton
              onClick={() => void handleNewChat()}
              disabled={isCreatingChat}
              tooltip="New chat"
            >
              {isCreatingChat ? (
                <Spinner className="size-4" />
              ) : (
                <MessageSquarePlus />
              )}
              <span>New chat</span>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarGroup>
        <SidebarGroup>
          <SidebarGroupLabel>History</SidebarGroupLabel>
          <ChatHistoryList
            activeChatId={activeChatId}
            chatList={chatList}
            deletingChatId={deletingChatId}
            editingChatId={editingChatId}
            editingTitle={editingTitle}
            isLoading={isLoading}
            isOnChat={isOnChat}
            savingChatId={savingChatId}
            onCancelRename={handleCancelRename}
            onDelete={(chatId) => void handleDelete(chatId)}
            onEditingTitleChange={setEditingTitle}
            onFirstChat={() => void handleNewChat()}
            onRename={(chatId) => void handleRename(chatId)}
            onSelectChat={handleSelectChat}
            onStartRename={handleStartRename}
          />
        </SidebarGroup>

        <SidebarSeparator />
      </SidebarContent>

      <SidebarFooter>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              asChild
              isActive={pathname.startsWith('/settings')}
              tooltip="Settings"
            >
              <Link href="/settings">
                <Settings />
                <span>Settings</span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  );
}
