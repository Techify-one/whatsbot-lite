Crie uma nova release do WhatsBot-Lite no GitHub seguindo estes passos:

1. Descubra a versão atual pela última tag git: `git describe --tags --abbrev=0 --match "v*"` (se não houver tag, considere v0.0.0)
2. Incremente a versão patch (ex: 0.1.0 → 0.1.1). Se o argumento for "minor", incremente o minor (ex: 0.1.1 → 0.2.0). Se for "major", incremente o major (ex: 0.2.0 → 1.0.0)
3. Rode `git status` e `git diff` para ver se há mudanças não commitadas
4. Gere o changelog automático: liste os commits desde a última tag de release (`git log <última_tag>..HEAD --oneline --no-merges`). Se não houver tag anterior, use os últimos 20 commits
5. Atualize o arquivo `WHATSBOT_VERSION` (JSON na raiz, lido por `/api/update` no self-update do painel):
   - Troque o campo `"version"` de nível superior para a nova versão
   - **Adicione** (não substitua) uma nova entrada no INÍCIO da lista `"changelog"`, com a nova versão e uma descrição enxuta (bullets em texto puro, sem markdown) baseada no changelog do passo 4. As entradas de versões anteriores continuam no array para preservar o histórico local; a entrada da nova versão também serve de fallback para o aviso quando a API do GitHub atinge o limite
   ```json
   {
     "version": "{nova_versão}",
     "changelog": [
       { "version": "{nova_versão}", "description": "- item 1\n- item 2\n..." },
       { "version": "{versão_anterior}", "description": "..." }
     ]
   }
   ```
   Esse arquivo viaja dentro do zip da tag — é ele que o botão "Atualizar" do painel usa para saber a versão instalada e é também o fallback do changelog quando a API do GitHub atinge o limite. O aviso de nova versão compara esse número com a última release e guarda as escolhas do usuário no banco da instalação. **Sempre bumpe `version` junto com a tag**, senão o self-update e o aviso deixam de refletir a versão real.
6. Faça commit de `WHATSBOT_VERSION` junto com qualquer outra mudança pendente (git add + commit com mensagem descritiva, ex: `chore(release): bump WHATSBOT_VERSION para {nova_versão}`)
7. Push para origin na branch main de `Techify-one/whatsbot-lite` (nome atual do repositório; não publicar em outro remote)
8. Crie a release no GitHub via `gh release create`, apontando para o commit que acabou de subir (que já contém o `WHATSBOT_VERSION` bumpado):
   ```bash
   gh release create v{nova_versão} --title "v{nova_versão}" --notes "## O que mudou

   - descrição do commit 1
   - descrição do commit 2
   ..." --latest
   ```
   Isso cria a tag, a release e marca como latest automaticamente.
9. Mostre o link do release retornado pelo `gh`

Argumento recebido: $ARGUMENTS
