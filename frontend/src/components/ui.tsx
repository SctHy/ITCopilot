// Business presentation helpers composed from the official shadcn/Base UI primitives.
import { useId, type ReactElement, type ReactNode, type Ref } from 'react'
import { ChevronDownIcon, CircleAlertIcon, InfoIcon } from 'lucide-react'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Card, CardHeader, CardContent, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Input } from '@/components/ui/input'
import { Select, SelectItem, SelectContent, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'

export type Tone = 'default' | 'accent' | 'success' | 'warning' | 'danger'

export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <h1 className="font-semibold text-2xl tracking-tight">{title}</h1>
        {description && <p className="mt-1.5 max-w-3xl text-muted-foreground text-sm leading-relaxed">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  )
}

export function Panel({ title, icon, actions, children, className = '', bodyClassName = '' }: {
  title?: ReactNode; icon?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; bodyClassName?: string
}) {
  return (
    <Card className={cn('gap-0 py-0', className)}>
      {(title || actions) && (
        <CardHeader className="flex h-12 flex-row items-center justify-between gap-3 border-b px-4 py-0">
          <div className="flex min-w-0 items-center gap-2 text-muted-foreground [&>svg]:size-4">
            {icon}
            {title && <CardTitle className="truncate font-semibold text-foreground text-base">{title}</CardTitle>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-1">{actions}</div>}
        </CardHeader>
      )}
      <CardContent className={cn('px-4 py-4', bodyClassName)}>{children}</CardContent>
    </Card>
  )
}

const toneClass = { default: '', accent: '[&>svg]:text-info-foreground', success: '[&>svg]:text-success-foreground', warning: '[&>svg]:text-warning-foreground', danger: '[&>svg]:text-destructive' } as const
const toneDot: Record<Tone, string> = { default: 'bg-muted-foreground/60', accent: 'bg-info', success: 'bg-success', warning: 'bg-warning', danger: 'bg-destructive' }

export function StatusChip({ tone = 'default', children }: { tone?: Tone; children: ReactNode; size?: 'sm' | 'md' }) {
  return (
    <Badge variant="outline" className={cn('gap-1.5 font-normal', {
      default: 'bg-secondary text-muted-foreground',
      accent: 'border-info/20 bg-info/10 text-info',
      success: 'border-success/20 bg-success/10 text-success',
      warning: 'border-warning/20 bg-warning/10 text-warning',
      danger: 'border-destructive/20 bg-destructive/10 text-destructive',
    }[tone])}>
      <span className={cn('size-1.5 rounded-full', toneDot[tone])} />{children}
    </Badge>
  )
}

export function ErrorAlert({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <Alert variant="destructive" role="alert">
      <CircleAlertIcon />
      {title ? <AlertTitle>{title}</AlertTitle> : null}
      <AlertDescription className="break-words">{children}</AlertDescription>
    </Alert>
  )
}

export function InfoAlert({ children, title, status = 'default' }: { children: ReactNode; title?: string; status?: 'default' | 'accent' | 'success' | 'warning' }) {
  return (
    <Alert className={toneClass[status]}>
      <InfoIcon />
      {title ? <AlertTitle>{title}</AlertTitle> : null}
      <AlertDescription>{children}</AlertDescription>
    </Alert>
  )
}

export function Empty({ icon, title, description, children }: { icon?: ReactNode; title: string; description?: ReactNode; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1.5 px-6 py-8 text-center">
      {icon && <span className="mb-2 text-muted-foreground [&>svg]:size-4.5">{icon}</span>}
      <p className="font-medium text-sm">{title}</p>
      {description && <p className="max-w-sm text-muted-foreground text-sm">{description}</p>}
      {children}
    </div>
  )
}

interface FieldProps {
  label?: string
  ariaLabel?: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
  description?: ReactNode
  isRequired?: boolean
  isDisabled?: boolean
  maxLength?: number
  className?: string
}

export function TextInput({ label, ariaLabel, value, onChange, placeholder, description, isRequired, isDisabled, maxLength, className = '', type = 'text', inputRef, min, max }:
  FieldProps & { type?: string; inputRef?: Ref<HTMLInputElement>; min?: number; max?: number }) {
  const id = useId()
  return (
    <div className={cn('grid w-full gap-2', className)}>
      {label && <Label htmlFor={id}>{label}{isRequired && <span className="text-destructive-foreground">*</span>}</Label>}
      <Input id={id} ref={inputRef} type={type} value={value} onChange={event => onChange(event.target.value)} placeholder={placeholder}
        required={isRequired} disabled={isDisabled} maxLength={maxLength} min={min} max={max} aria-label={label ? undefined : ariaLabel} />
      {description && <p className="text-sm text-muted-foreground">{description}</p>}
    </div>
  )
}

export function TextAreaInput({ label, ariaLabel, value, onChange, placeholder, description, isRequired, isDisabled, maxLength, className = '', rows = 3, inputRef, onKeyDown }:
  FieldProps & { rows?: number; inputRef?: Ref<HTMLTextAreaElement>; onKeyDown?: (event: React.KeyboardEvent<HTMLTextAreaElement>) => void }) {
  const id = useId()
  return (
    <div className={cn('grid w-full gap-2', className)}>
      {label && <Label htmlFor={id}>{label}{isRequired && <span className="text-destructive-foreground">*</span>}</Label>}
      <Textarea id={id} ref={inputRef} rows={rows} value={value} onChange={event => onChange(event.target.value)} placeholder={placeholder}
        required={isRequired} disabled={isDisabled} maxLength={maxLength} onKeyDown={onKeyDown} aria-label={label ? undefined : ariaLabel} />
      {description && <p className="text-sm text-muted-foreground">{description}</p>}
    </div>
  )
}

export interface Option { id: string; label: string }

export function SelectInput({ label, ariaLabel, value, onChange, options, placeholder = '请选择', isDisabled, isRequired, className = '' }: {
  label?: string; ariaLabel?: string; value: string; onChange: (value: string) => void; options: Option[]; placeholder?: string; isDisabled?: boolean; isRequired?: boolean; className?: string
}) {
  const items = options.map(option => ({ value: option.id, label: option.label }))
  const id = useId()
  return (
    <div className={cn('grid w-full gap-2', className)}>
      {label && <Label htmlFor={id}>{label}{isRequired && <span className="text-destructive-foreground">*</span>}</Label>}
      <Select items={items} value={value || null} onValueChange={next => onChange(next ?? '')} disabled={isDisabled} required={isRequired}>
        <SelectTrigger id={id} className="w-full" aria-label={label ? undefined : ariaLabel}>
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false} align="start">
          {items.map(item => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}
        </SelectContent>
      </Select>
    </div>
  )
}

export function MultiSelectInput({ label, values, onChange, options, placeholder = '可多选', className = '' }: {
  label: string; values: string[]; onChange: (values: string[]) => void; options: Option[]; placeholder?: string; className?: string
}) {
  const id = useId()
  const items = options.map(option => ({ value: option.id, label: option.label }))
  return (
    <div className={cn('grid w-full gap-2', className)}>
      <Label htmlFor={id}>{label}</Label>
      <Select items={items} multiple value={values} onValueChange={onChange}>
        <SelectTrigger id={id} className="w-full">
          <SelectValue placeholder={placeholder}>
            {(selected: string[]) => selected.length ? selected.map(value => options.find(option => option.id === value)?.label ?? value).join('、') : placeholder}
          </SelectValue>
        </SelectTrigger>
        <SelectContent alignItemWithTrigger={false} align="start">
          {items.map(item => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}
        </SelectContent>
      </Select>
    </div>
  )
}

export function Mono({ children }: { children: ReactNode }) {
  return <pre className="code-block">{children}</pre>
}

/** Key/value grid used by approval cards and metric tiles. */
export function Facts({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
      {items.map(([key, value], index) => (
        <div key={index} className="contents">
          <dt className="text-muted-foreground">{key}</dt>
          <dd className="min-w-0 break-words">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

export const errorText = (error: unknown) => error instanceof Error ? error.message : String(error)

/** Tooltip around a single trigger element (usually an icon button). */
export function Hint({ label, children, side = 'top' }: { label: ReactNode; children: ReactElement; side?: 'top' | 'bottom' | 'left' | 'right' }) {
  return (
    <Tooltip>
      <TooltipTrigger render={children} />
      <TooltipContent side={side}>{label}</TooltipContent>
    </Tooltip>
  )
}

/** Disclosure: a ghost-button trigger with a rotating chevron, and a collapsible panel. */
export function Collapse({ trigger, children, open, onOpenChange, defaultOpen, className = '', triggerClassName = '', panelClassName = '' }: {
  trigger: ReactNode; children: ReactNode; open?: boolean; onOpenChange?: (open: boolean) => void; defaultOpen?: boolean
  className?: string; triggerClassName?: string; panelClassName?: string
}) {
  return (
    <Collapsible open={open} onOpenChange={onOpenChange} defaultOpen={defaultOpen} className={className}>
      <CollapsibleTrigger render={<Button type="button" variant="ghost" size="sm" className={cn('group text-muted-foreground', triggerClassName)} />}>
        {trigger}
        <ChevronDownIcon className="size-3.5 transition-transform group-data-panel-open:rotate-180" />
      </CollapsibleTrigger>
      <CollapsibleContent className={panelClassName}>{children}</CollapsibleContent>
    </Collapsible>
  )
}
