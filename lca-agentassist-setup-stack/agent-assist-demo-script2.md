# LCA AGENTASSIST Demo

***
### Part 1: AgentAssist gathers some information for later use - eg credit card type and caller’s first name.       
***

**Agent**: Hello! Thanks for calling ACME Bank. My name is Babu. How can i help you?  
**Caller**: Hi. I’m calling about my Rewards card.   
**Agent**: Excellent.. give me a second to look up your customer account..    
  
<span style="color:blue">**AGENTASSIST**:  Ask caller to state their first name only, for verification purposes. It should be ‘Bob’ per callerId lookup.</span>  
   
**Agent**: For verification purposes, can you please state your first name?  
**Caller**: My first name is Bob.   
**Agent**: one moment while we verify* that your name matches our records.*    

<span style="color:blue">**AGENTASSIST**: Suggested response: “Perfect! Hi Bob. How can i help you with your rewards-card?”</span> 

**Agent**:  It matches - perfect! Hi Bob. How can i help you with your rewards-card?*

***
### Part 2: AgentAssist retrieves information using the information gathered earlier (eg card type).. in this case by using conditional answer in QnAbot, but other mechanisms -  such as Kendra redirect query filters or custom logic with Lambda hooks - could also be used.
***

**Caller**: I was wondering, what is the cash back rate for this card?  
**Agent**:  I’m looking that up for you now. It won’t take long.   

<span style="color:blue">**AGENTASSIST**: Suggested response: Yes, I see here that the rewards card has a 2% cashback rate on your everyday spending. Did that answer your question? Anything else I can help with today?</span>
  
**Agent**: Yes, I see here that the rewards card has a 2% cashback rate on your everyday spending. Did that answer your question? Anything else I can help with today?  

***
### Part 3: AgentAssist can guide agent through an online transaction, prompting agent to collect the needed information. Potentially AgentAssist could fully automate transaction fulfillment.
***

**Caller**: Thanks, yes. Can i pay my card bill now while I’m on the phone?  
**Agent**:  Yes, absolutely, we can process your payment over the phone. Give me a second to set that up.   

<span style="color:blue">**AGENTASSIST**: Ask caller for last 4 digits of card number</span>
  
**Agent**: Can you please confirm the last 4 digits of your card number?  
**Caller**: Yes. It’s 1234  
**Agent**:  Got it - 1234.    
  
<span style="color:blue">**AGENTASSIST**: Confirm card balance (eg $75.34). Ask caller how much they want to pay ($50 minimum)</span>
  
**Agent**: Great. I see that your card balance is $75.34. How much do you want to pay? There’s a $50 minimum payment.  
**Caller**: $50  
**Agent**:  OK, I’m entering payment for $50.   
  
<span style="color:blue">**AGENTASSIST**: Ask caller if they want to pay from their checking or savings account</span>
  
**Agent**: Which account do you want to pay that from.. Your Savings account, or your Checking account?  
**Caller**: My checking account.  
**Agent**:  Your checking account - perfect. One moment please.    
  
<span style="color:blue">**AGENTASSIST**: All information captured.. Last 4 = 1234, Amount = $50, Account = Checking</span>
  
**Agent**: Great, so I’ve processed payment of $50 from your Checking account.  Is there anything else I can help you with?  

***
### Part 4: Caller asks a question where we have an Kendra FAQ answer in QnABot
***

**Caller**: Can you tell me what kinds of life insurance policies you have?   
**Agent**:  Yes, of course. I’m just pulling up our latest life insurance policy information now.   
  
<span style="color:blue">**AGENTASSIST**: Open https://en.wikipedia.org/wiki/Life_insurance: We offer many types of Term life insurance, and Permanent life insurance, including Whole life, Universal life, Endowment policies, Accidental death policies, and more.</span>
  
**Agent**: We offer many types of Term life insurance, and Permanent life insurance, including Whole life, Universal life, Endowment policies, Accidental death policies, and more.  

***
### Part 5: Caller asks a question for which we don’t have an FAQ.. Demonstrates fallback to a Kendra query on unstructured docs from the index (in the case the crawled website)
***

**Caller**: Interesting. What is a universal life policy?  
**Agent**: I'll tell you in one second!   
  
<span style="color:blue">**AGENTASSIST**: Universal life insurance is a type of permanent life insurance policy that combines a death benefit with a savings component by allowing policyholders to vary premium payments and coverage amounts. The cash value portion of the policy grows tax deferred.
Source Links: Life insurance - Wikipedia, Mortgage loan - Wikipedia </span>

**Agent**: Universal life insurance is a type of permanent life insurance policy that combines a death benefit with a cash value savings component. Universal life policies are flexible in that lot of policy holders can change coverage amounts and premium payments over time. 

**Caller**: What is its coverage period?
  
<span style="color:blue">**AGENTASSIST**: "Universal life insurance provides lifetime coverage, with no fixed end date for the death benefit coverage."
Source Links: Life insurance - Wikipedia </span>

**Agent**: Universal life insurance olicies typically provide life time coverage as long as premiums are paid.  

**Caller**: Do they have cash value?
  
<span style="color:blue">**AGENTASSIST**: Yes, universal life insurance policies do have cash values. The references state: "Universal life insurance policies have cash values. Paid-in premiums increase their cash values; administrative and other costs reduce their cash values."
Source Links: Life insurance - Wikipedia </span>
  
**Agent**: Yes, universal life insurance policies do have cash value savings component. Paid-in premiums increase the cash value.

***
### Part 6: Wrapup
***

**Caller**: That sounds great. Thanks.  
**Agent**: Is there anything else I can help you with today?  
**Caller**: Nothing else just now. You’ve been a big help. Thank you so much.  
**Agent**: No problem. I am very glad to hear you say that I was able to help you today.   
  
<span style="color:blue">**AGENTASSIST**: Thanks Bob! We appreciate your business. Check out our website at ACMEBank.com to learn about all our other products, and feel free to call back if you have any additional questions. Have a great day.</span>
  
**Agent**: Thanks Bob! We appreciate your business. Check out our website at [ACMEBank.com](http://acme.com/) to learn about all our other products, and feel free to call back if you have any additional questions. Have a great day.  
**Caller**: Thanks. Bye.  
**Agent**: Goodbye.  
  





 


