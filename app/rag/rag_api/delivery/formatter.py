"""A conservative Markdown subset translated into visible text and entities."""
import re
from urllib.parse import urlsplit

from rag.rag_api.config import trusted_base_url
from rag.rag_api.contracts import ClaimedDelivery, DeliveryPart, ImagePart, InlineAction, TelegramEntity, TextPart
from rag.rag_api.delivery._texts import FALLBACK_TEXT


def units(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def clip(text: str, budget: int) -> str:
    result = []
    for char in text:
        size = units(char)
        if size > budget:
            break
        result.append(char)
        budget -= size
    return ''.join(result)


def safe_link(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError:
        return False
    return (parsed.scheme in ('https', 'http') and bool(parsed.hostname)
            and not parsed.username and not parsed.password
            and not any(c.isspace() or ord(c) < 32 for c in url))


def markdown(text: str) -> tuple[str, list[TelegramEntity]]:
    """Render supported spans recursively; unsupported constructs stay literal.

    Raw HTML is plain text. Code never contains nested entities. Links with
    credentials or non-HTTP schemes are rendered as labels only.
    """
    text = re.sub(r'(?m)^ {0,3}#{1,6}\s+', '', text)

    def render(source, depth=0):
        output, entities = [], []
        position, index = 0, 0

        def append(value):
            nonlocal position
            output.append(value)
            position += units(value)

        while index < len(source):
            if source[index] == '\\' and index + 1 < len(source):
                append(source[index + 1])
                index += 2
                continue
            if depth < 16 and source[index] == '[':
                match = re.match(r'\[([^\]\n]+)\]\(([^\s)]+)\)', source[index:])
                if match:
                    label, nested = render(match[1], depth + 1)
                    start = position
                    append(label)
                    entities.extend(e.model_copy(update={'offset': e.offset + start}) for e in nested)
                    if label and safe_link(match[2]):
                        entities.append(TelegramEntity(type='text_link', offset=start,
                                                       length=units(label), url=match[2]))
                    index += len(match[0])
                    continue
            handled = False
            for marker, kind in [('```', 'pre'), ('`', 'code'), ('**', 'bold'),
                                 ('__', 'bold'), ('*', 'italic'), ('_', 'italic')]:
                if depth >= 16 or not source.startswith(marker, index):
                    continue
                end = source.find(marker, index + len(marker))
                if end <= index + len(marker):
                    continue
                inner = source[index + len(marker):end]
                if kind == 'pre':
                    inner = re.sub(r'^[A-Za-z0-9_+-]*\n', '', inner, count=1)
                visible, nested = (inner, []) if kind in ('code', 'pre') else render(inner, depth + 1)
                start = position
                append(visible)
                if visible:
                    entities.append(TelegramEntity(type=kind, offset=start, length=units(visible)))
                    entities.extend(e.model_copy(update={'offset': e.offset + start}) for e in nested)
                index = end + len(marker)
                handled = True
                break
            if not handled:
                append(source[index])
                index += 1
        return ''.join(output), sorted(entities, key=lambda e: (e.offset, -e.length))

    return render(text)


def button(text, data):
    return InlineAction(text=text, callback_data=data)


def _build(delivery: ClaimedDelivery, source_base_url: str) -> list[DeliveryPart]:
    delivery = ClaimedDelivery.model_validate(delivery.model_dump())
    base = trusted_base_url(source_base_url)
    if delivery.parts is not None:
        return list(delivery.parts)
    purpose = delivery.purpose
    variant = 'normal'
    actions, images, entities = [], [], []
    if purpose in ('answer', 'notice'):
        result = delivery.content
        allowed = {str(image.image_id): image for image in result.images}

        def replace_image(match):
            image = allowed.get(match[1].lower())
            if image is None:
                raise ValueError('unknown_image')
            # Escape description so a saved image description cannot introduce
            # extra Markdown entities or image URLs.
            return re.sub(r'([\\`*_\[\]])', r'\\\1', image.description)

        raw = re.sub(r'!\[(?:\\.|[^\]\\])*\]\(image://([^)]+)\)', replace_image, result.answer_text)
        if 'image://' in raw:
            raise ValueError('invalid_image_marker')
        text, entities = markdown(raw)
        images = [ImagePart(kind='image', image=image) for image in result.images]
        suffix, source_entities, seen = '', [], set()
        for source in result.sources:
            if source.document_id in seen:
                continue
            seen.add(source.document_id)
            prefix = '\n\nИсточники:\n' if not suffix else '\n'
            remaining = 512 - units(suffix + prefix)
            if remaining <= 0:
                break
            name = clip(source.source_name, min(160, remaining))
            if not name:
                break
            offset = units(suffix + prefix)
            suffix += prefix + name
            source_entities.append(TelegramEntity(type='text_link', offset=offset,
                length=units(name), url=base + '/sources/' + str(source.document_id)))
        if units(text + suffix) > 4096:
            # Keep the complete answer and one compact source when possible.
            remaining = 4096 - units(text) - 1
            name = clip(result.sources[0].source_name, min(160, max(0, remaining))) if result.sources else ''
            if name:
                suffix = '\n' + name
                source_entities = [TelegramEntity(type='text_link', offset=1,
                    length=units(name), url=base + '/sources/' + str(result.sources[0].document_id))]
            else:
                text, suffix, entities, source_entities, images = FALLBACK_TEXT, '', [], [], []
                variant = 'length_fallback'
        length = units(text)
        entities.extend(e.model_copy(update={'offset': e.offset + length}) for e in source_entities)
        text += suffix
        if purpose == 'answer' and delivery.request_id is not None:
            actions = [button('Ответ не помог', f'a:{delivery.request_id}')]
    elif purpose == 'support_offer':
        text = 'Если помощь ещё нужна, позовите оператора.'
        actions = [button('Позвать оператора', f's:{delivery.request_id}')]
    elif purpose == 'support_confirmation':
        text = 'Помощь ещё нужна?'
        actions = [button('Да', f'y:{delivery.case.case_id}'), button('Нет', f'n:{delivery.case.case_id}')]
    elif purpose == 'ticket_link':
        text = 'Обращение в поддержку создано.'
        # Reconstruct from the trusted base rather than accepting any stored URL.
        actions = [InlineAction(text='Открыть тикет', url=base + '/tickets/' + str(delivery.case.case_id))]
    else:
        text = {
            'status': 'Вопрос в работе. Сообщу, когда ответ будет готов.',
            'busy': 'Предыдущий вопрос ещё обрабатывается. Дождитесь ответа.',
            'denied': 'Доступ к боту не предоставлен. Обратитесь к администратору.',
            'first_question': 'Сначала задайте вопрос и дождитесь ответа бота.',
        }[purpose]
    return [TextPart(kind='text', text=text or 'Ответ не содержит текста.', variant=variant,
                     entities=entities, actions=actions), *images]
