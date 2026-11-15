const rules = new Intl.PluralRules('en');

/** "1 analog site" / "5 analog sites". Never hard-code the plural. */
export const plural = (n: number, one: string, other: string) => `${n} ${rules.select(n) === 'one' ? one : other}`;
